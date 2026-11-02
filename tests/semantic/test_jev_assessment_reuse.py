"""Synthetic regressions for cached and partial primary Jev assessments."""
import json
import time

import pytest

import semantic_drain as drain
import semantic_jev as jev
import semantic_policy
import semantic_store
import semantic_runtime
from inference_version import CURRENT_VERSION, VERSION_KEY
from semantic_testkit import _FakeJev, _cfg, _seeded
from test_patient_context_refresh import runtime as runtime
from test_patient_context_refresh import seed


def _complete(scfg):
    return {name: {'noul': 0.9, 'verdict': 'MATCH'} for name in jev.active_propositions(scfg)}


@pytest.mark.parametrize('fault', ['missing', 'target', 'model', 'registry', 'content', 'source', 'policy', 'stale', 'invalidated'])
def test_corrupt_complete_assessment_never_reaches_following_stage(tmp_path, monkeypatch, fault):
    db = _seeded(tmp_path)
    cfg = _cfg('shadow', summary_mode='off', loop_mode='off', extra_propositions=['E01'])
    scfg = semantic_policy.runtime_config(cfg)[0]
    real = drain._primary_assessment_history
    seen = []

    def artifact(database, pid, mid, fp, policy):
        if mid != 1:
            return real(database, pid, mid, fp, policy)
        seen.append(True)
        content = {'target_message_id': mid, 'verdicts': _complete(scfg)}
        meta = {VERSION_KEY: CURRENT_VERSION, 'fingerprint': fp, 'policy_fingerprint': policy,
                'schema': drain.SCHEMA_VERSION, 'technical_status': 'complete',
                'model': jev.JEV_MODEL, 'registry': jev.REGISTRY_VERSION}
        model = jev.JEV_MODEL
        if fault == 'missing':
            del content['verdicts']['E01']
        elif fault == 'target':
            content['target_message_id'] = 2
        elif fault == 'model':
            model = 'synthetic-other-model'
        elif fault == 'registry':
            meta['registry'] = 'wrong'
        elif fault == 'content':
            content['verdicts']['E01']['verdict'] = 'NO_MATCH'
        elif fault == 'source':
            meta['fingerprint'] = 'wrong'
        elif fault == 'policy':
            meta['policy_fingerprint'] = 'wrong'
        else:
            meta[fault] = True
        return {'content': content, 'meta': meta, 'model': model}, []

    monkeypatch.setattr(drain, '_primary_assessment_history', artifact)
    client = _FakeJev()
    try:
        result = drain.run_due(db, cfg, {'errors': []}, time.monotonic() + 300,
                               jev_client=client, llm_fn=lambda _: pytest.fail('must not call LLM'))
        assert seen and result['failed'] == 1
        assert client.calls == []
    finally:
        db.close()


@pytest.mark.parametrize('old_noul', [0.9, 0.1])
def test_retry_preserves_existing_partial_and_only_asks_missing(runtime, old_noul):
    db, _, _ = runtime
    seed(db, 1, proof=False)
    cfg = semantic_policy.runtime_config(_cfg('assist', extra_propositions=['E01']))[0]
    bundle = semantic_store.thread_bundle(db.db, 1, 1, [1])
    old = {'P01': {'noul': old_noul, 'verdict': 'MATCH' if old_noul == 0.9 else 'NO_MATCH'}}
    prev = {'content': {'target_message_id': 1, 'verdicts': old}, 'meta': {'technical_status': 'retry_wait'}}
    before = len(db.artifacts(drain.KIND_ASSESS))
    diagnostics = drain._job_diagnostics()
    client = _FakeJev(noul=0.91)
    verdicts, flag = drain._assess_target(db, cfg, bundle, client, 1, 1,
        bundle['source_fingerprint'], semantic_policy.policy_fingerprint(cfg), prev,
        time.monotonic() + 60, diagnostics=diagnostics)
    assert flag is None and verdicts['P01'] == old['P01']
    assert len(db.artifacts(drain.KIND_ASSESS)) == before + 1
    assert all('P01' not in call for call in client.calls)



def test_genuine_prefilter_is_reusable_only_for_current_greeting(runtime):
    db, _, _ = runtime
    seed(db, 1, proof=False)
    db.db.execute("UPDATE messages SET body_text='こんにちは' WHERE message_id=1")
    content_hash = db.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()[0]
    db.artifact_add('extract_llm', '{}', project_id=1, message_id=1,
                    meta={'hash': content_hash, 'prefilter': 'no_signal', VERSION_KEY: CURRENT_VERSION})
    assert drain._verified_prefilter(db, 1, 1)
    cfg = semantic_policy.runtime_config(_cfg('assist', extra_propositions=['E01']))[0]
    bundle = semantic_store.thread_bundle(db.db, 1, 1, [1])
    fp, policy = bundle['source_fingerprint'], semantic_policy.policy_fingerprint(cfg)
    client = _FakeJev()
    verdicts, flag = drain._assess_target(db, cfg, bundle, client, 1, 1, fp, policy, None,
                                         time.monotonic() + 60, diagnostics=drain._job_diagnostics())
    assert verdicts == {} and flag is None and not client.calls
    row, _ = drain._primary_assessment_history(db.db, 1, 1, fp, policy)
    assert drain._valid_primary_assessment(row['content'], row['meta'], row['model'], 1, fp, policy, cfg,
                                           prefiltered=drain._verified_prefilter(db, 1, 1))
    report = drain.proposition_quality_report(db, 1, cfg)
    assert report['messages'] == 0 and report['prefiltered'] == 1 and report['superseded'] == 0
    assert all(prop['noul_mean'] is None for prop in report['propositions'].values())
    db.artifact_add('extract_llm', '{}', project_id=1, message_id=1,
                    meta={'hash': 'stale-synthetic-hash', 'prefilter': 'no_signal', VERSION_KEY: CURRENT_VERSION})
    assert not drain._verified_prefilter(db, 1, 1)
    db.artifact_add('extract_llm', '{}', project_id=1, message_id=1,
                    meta={'hash': content_hash, 'prefilter': 'no_signal', VERSION_KEY: CURRENT_VERSION})
    db.db.execute("UPDATE messages SET body_text='合成の薬を増量します' WHERE message_id=1")
    assert not drain._verified_prefilter(db, 1, 1)
    assert not drain._valid_primary_assessment(row['content'], row['meta'], row['model'], 1, fp, policy, cfg,
                                               prefiltered=drain._verified_prefilter(db, 1, 1))


def test_repeated_partial_failure_resumes_only_unanswered_and_keeps_receipts(runtime):
    db, _, _ = runtime
    seed(db, 1, proof=False)
    cfg = semantic_policy.runtime_config(_cfg('assist', extra_propositions=['E01'],
                                               max_questions_per_request=2))[0]
    bundle = semantic_store.thread_bundle(db.db, 1, 1, [1])
    fp, policy = bundle['source_fingerprint'], semantic_policy.policy_fingerprint(cfg)

    class FailAfter(_FakeJev):
        def __init__(self, successful):
            super().__init__()
            self.successful = successful
        def evaluate(self, state, questions, deadline):
            if len(self.calls) >= self.successful:
                self.calls.append(sorted(questions))
                self.requests_made += 1
                raise jev.JevError('transport', 'synthetic', retryable=True)
            return super().evaluate(state, questions, deadline)

    first = FailAfter(1)
    verdicts, flag = drain._assess_target(db, cfg, bundle, first, 1, 1, fp, policy, None,
                                          time.monotonic() + 60, diagnostics=drain._job_diagnostics())
    assert verdicts is None and flag == 'retryable'
    prev, _ = drain._primary_assessment_history(db.db, 1, 1, fp, policy)
    saved = dict(prev['content']['verdicts'])
    assert len(saved) == 2 and prev['meta']['jev_requests'] == 2
    second = FailAfter(0)
    verdicts, flag = drain._assess_target(db, cfg, bundle, second, 1, 1, fp, policy, prev,
                                          time.monotonic() + 60, diagnostics=drain._job_diagnostics())
    assert verdicts is None and flag == 'retryable'
    assert not set(saved).intersection(second.calls[0])
    prev, _ = drain._primary_assessment_history(db.db, 1, 1, fp, policy)
    assert prev['content']['verdicts'] == saved
    final = _FakeJev(noul=0.91)
    verdicts, flag = drain._assess_target(db, cfg, bundle, final, 1, 1, fp, policy, prev,
                                          time.monotonic() + 60, diagnostics=drain._job_diagnostics())
    assert flag is None and all(verdicts[key] == value for key, value in saved.items())
    assert all(not set(saved).intersection(call) for call in final.calls)
    assert set(verdicts) == set(jev.active_propositions(cfg))


@pytest.mark.parametrize('damage', ['invalidated', 'stale', 'bad_meta', 'bad_content', 'missing_binding'])
def test_newer_damaged_primary_never_falls_back_to_older_valid(runtime, damage):
    db, _, _ = runtime
    seed(db, 1, proof=False)
    cfg = semantic_policy.runtime_config(_cfg('assist', extra_propositions=['E01']))[0]
    bundle = semantic_store.thread_bundle(db.db, 1, 1, [1])
    fp, policy = bundle['source_fingerprint'], semantic_policy.policy_fingerprint(cfg)
    drain._assess_target(db, cfg, bundle, _FakeJev(), 1, 1, fp, policy, None,
                         time.monotonic() + 60, diagnostics=drain._job_diagnostics())
    row, _ = drain._primary_assessment_history(db.db, 1, 1, fp, policy)
    content, meta = dict(row['content']), dict(row['meta'])
    if damage in ('stale', 'invalidated'):
        meta[damage] = True
    elif damage == 'missing_binding':
        del meta['policy_fingerprint']
    aid = db.artifact_add(drain.KIND_ASSESS, json.dumps(content), project_id=1, message_id=1,
                          model=jev.JEV_MODEL, meta=meta)
    if damage in ('bad_meta', 'bad_content', 'missing_binding'):
        if damage != 'missing_binding':
            column = 'meta' if damage == 'bad_meta' else 'content'
            db.db.execute(f'UPDATE artifacts SET {column}=? WHERE artifact_id=?', ('{broken', aid))
        with pytest.raises((ValueError, TypeError)):
            drain._primary_assessment_history(db.db, 1, 1, fp, policy)
    else:
        newest, _ = drain._primary_assessment_history(db.db, 1, 1, fp, policy)
        assert not drain._valid_primary_assessment(newest['content'], newest['meta'], newest['model'],
                                                   1, fp, policy, cfg)


@pytest.mark.parametrize('damage', ['error', 'bad_content'])
def test_newest_prefilter_row_must_itself_be_valid(runtime, damage):
    db, _, _ = runtime
    seed(db, 1, proof=False)
    db.db.execute("UPDATE messages SET body_text='こんにちは' WHERE message_id=1")
    content_hash = db.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()[0]
    meta = {'hash': content_hash, 'prefilter': 'no_signal', VERSION_KEY: CURRENT_VERSION}
    db.artifact_add('extract_llm', '{}', project_id=1, message_id=1, meta=meta)
    assert drain._verified_prefilter(db, 1, 1)
    if damage == 'error':
        meta['error'] = 1
    aid = db.artifact_add('extract_llm', '{}', project_id=1, message_id=1, meta=meta)
    if damage == 'bad_content':
        db.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', ('{broken', aid))
    assert not drain._verified_prefilter(db, 1, 1)


def test_resumed_questions_still_obey_guard_and_reservation(runtime):
    db, _, _ = runtime
    seed(db, 1, proof=False)
    cfg = semantic_policy.runtime_config(_cfg('assist', extra_propositions=['E01']))[0]
    bundle = semantic_store.thread_bundle(db.db, 1, 1, [1])
    fp, policy = bundle['source_fingerprint'], semantic_policy.policy_fingerprint(cfg)
    prev = {'content': {'target_message_id': 1, 'verdicts': {'P01': {'noul': 0.9, 'verdict': 'MATCH'}}},
            'meta': {'technical_status': 'retry_wait'}}
    guards, receipts = [], []

    class Guarded(_FakeJev):
        supports_attempt_hooks = True
        def evaluate(self, state, questions, deadline, *, before_attempt, after_result, reserve_fn, transport):
            reserve_fn(b'synthetic', 1.0)
            before_attempt()
            result = super().evaluate(state, questions, deadline)
            after_result()
            return result

    raw = Guarded(noul=0.91)
    client = semantic_runtime.bind_jev(raw, guards.append, reserve=lambda body, timeout: receipts.append('reserved'))
    verdicts, flag = drain._assess_target(db, cfg, bundle, client, 1, 1, fp, policy, prev,
                                          time.monotonic() + 60, diagnostics=drain._job_diagnostics())
    assert flag is None and verdicts['P01']['noul'] == 0.9
    assert receipts and len(receipts) == len(raw.calls)
    assert guards.count('jev_attempt') == guards.count('jev_result') == len(raw.calls)
    assert all('P01' not in call for call in raw.calls)


def test_job_does_not_reuse_older_valid_row_after_invalidation(tmp_path):
    db = _seeded(tmp_path)
    cfg = _cfg('shadow', summary_mode='off', loop_mode='off', extra_propositions=['E01'])
    scfg = semantic_policy.runtime_config(cfg)[0]
    try:
        job = db.db.execute("SELECT * FROM fetch_jobs WHERE kind=? AND project_id=1 AND message_id=1",
                            (drain.JOB_KIND,)).fetchone()
        assert job is not None
        targets = json.loads(job['payload']).get('targets', [1])
        bundle = semantic_store.thread_bundle(db.db, 1, 1, targets)
        fp, policy = bundle['source_fingerprint'], semantic_policy.policy_fingerprint(scfg)
        drain._assess_target(db, scfg, bundle, _FakeJev(), 1, 1, fp, policy, None,
                             time.monotonic() + 60, diagnostics=drain._job_diagnostics())
        old, _ = drain._primary_assessment_history(db.db, 1, 1, fp, policy)
        db.artifact_add(drain.KIND_ASSESS, json.dumps(old['content']), project_id=1, message_id=1,
                        model=jev.JEV_MODEL, meta={**old['meta'], 'invalidated': True})
        client = _FakeJev()
        result = drain.run_due(db, cfg, {'errors': []}, time.monotonic() + 300,
                               jev_client=client, llm_fn=lambda _: pytest.fail('must not call LLM'))
        assert result['failed'] == 1 and not client.calls
    finally:
        db.close()


@pytest.mark.parametrize('missing', ['registry', 'model'])
def test_partial_answers_require_registry_and_model_evidence(runtime, missing):
    db, _, _ = runtime
    seed(db, 1, proof=False)
    cfg = semantic_policy.runtime_config(_cfg('assist', extra_propositions=['E01']))[0]
    bundle = semantic_store.thread_bundle(db.db, 1, 1, [1])
    fp, policy = bundle['source_fingerprint'], semantic_policy.policy_fingerprint(cfg)
    content = {'target_message_id': 1, 'verdicts': {'P01': {'noul': 0.9, 'verdict': 'MATCH'}}}
    meta = {VERSION_KEY: CURRENT_VERSION, 'fingerprint': fp, 'policy_fingerprint': policy,
            'schema': drain.SCHEMA_VERSION, 'technical_status': 'retry_wait',
            'model': jev.JEV_MODEL, 'registry': jev.REGISTRY_VERSION}
    assert drain._valid_primary_assessment(content, meta, jev.JEV_MODEL, 1, fp, policy, cfg)
    del meta[missing]
    assert not drain._valid_primary_assessment(content, meta, jev.JEV_MODEL, 1, fp, policy, cfg)


@pytest.mark.parametrize('damage', ['registry_missing', 'registry_wrong', 'meta_model_missing',
                                     'meta_model_wrong', 'row_model_wrong', 'status_missing', 'status_unknown', 'none'])
def test_prior_partial_provenance_is_required_after_later_completion(runtime, damage):
    from test_patient_context_refresh import _raw_proof

    db, _, _ = runtime
    seed(db, 1, proof=False)
    _raw_proof(db, 1, 1)
    cfg = semantic_policy.runtime_config(_cfg('assist', summary_mode='off', loop_mode='off',
                                               extra_propositions=['E01']))[0]
    bundle = semantic_store.thread_bundle(db.db, 1, 1, [1])
    fp, policy = bundle['source_fingerprint'], semantic_policy.policy_fingerprint(cfg)
    content = {'target_message_id': 1, 'verdicts': {'P01': {'noul': 0.9, 'verdict': 'MATCH'}}}
    meta = {VERSION_KEY: CURRENT_VERSION, 'fingerprint': fp, 'policy_fingerprint': policy,
            'schema': drain.SCHEMA_VERSION, 'technical_status': 'retry_wait',
            'model': jev.JEV_MODEL, 'registry': jev.REGISTRY_VERSION}
    model = jev.JEV_MODEL
    if damage == 'registry_missing':
        del meta['registry']
    elif damage == 'registry_wrong':
        meta['registry'] = 'synthetic-other-registry'
    elif damage == 'meta_model_missing':
        del meta['model']
    elif damage == 'meta_model_wrong':
        meta['model'] = 'synthetic-other-model'
    elif damage == 'row_model_wrong':
        model = 'synthetic-other-model'
    elif damage == 'status_missing':
        del meta['technical_status']
    elif damage == 'status_unknown':
        meta['technical_status'] = 'synthetic-unknown'
    db.artifact_add(drain.KIND_ASSESS, json.dumps(content), project_id=1, message_id=1,
                    model=model, meta=meta)
    result, flag = drain._assess_target(db, cfg, bundle, _FakeJev(), 1, 1, fp, policy, None,
                                         time.monotonic() + 60, diagnostics=drain._job_diagnostics())
    assert result is not None and flag is None
    current, previous = drain._primary_assessment_history(db.db, 1, 1, fp, policy)
    assert drain._valid_primary_history(current['content'], current['meta'], previous,
                                        1, fp, policy, cfg) is (damage == 'none')
    report = drain.proposition_quality_report(db, 1, cfg)
    assert report['messages'] == (1 if damage == 'none' else 0)
    assert report['superseded'] == (0 if damage == 'none' else 1)
    row = db.db.execute("SELECT m.*, p.project_id AS patient_id, p.fetch_reason "
                        "FROM messages m JOIN patients p ON p.project_id=m.project_id "
                        "WHERE m.message_id=1").fetchone()
    db.db.execute("UPDATE fetch_jobs SET state='done' WHERE state != 'done'")
    db.db.commit()
    assert drain._reprocess_target_state(db, row, cfg) == ('current' if damage == 'none' else 'held')
