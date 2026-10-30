"""Synthetic evidence and selection checks for context inference readiness."""
import json
import time

import pytest

import extract_llm
from inference_version import CURRENT_VERSION, VERSION_KEY
import patient_context_refresh as refresh
import semantic_policy
import semantic_drain
import semantic_store
from semantic_testkit import _cfg, _FakeJev, _llm
from test_patient_context_refresh import runtime as runtime
from test_patient_context_refresh import _raw_proof, _semantic_proof, seed, step


def _proof(led, pid, mid, cfg):
    _raw_proof(led, pid, mid)
    _semantic_proof(led, pid, mid, cfg)


def _ready(runtime, cfg, **kw):
    led, clock, _ = runtime
    return refresh._inference_ready(led, 1, cfg, deadline=clock.mono + 600, **kw)


def test_semantic_off_requires_complete_current_bound_extraction(runtime):
    led, _, _ = runtime
    cfg = {'semantic': {'mode': 'off'}}
    seed(led, 1, proof=False)
    assert not _ready(runtime, cfg)
    _raw_proof(led, 1, 1)
    assert _ready(runtime, cfg)
    led.db.execute("UPDATE messages SET posted_at_ts=posted_at_ts+1 WHERE message_id=1")
    led.db.commit()
    assert not _ready(runtime, cfg)
    _raw_proof(led, 1, 1)
    assert _ready(runtime, cfg)
    led.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.integrity.completeness_chunks',0) "
                   "WHERE artifact_id=(SELECT MAX(artifact_id) FROM artifacts WHERE kind='extract_llm')")
    led.db.commit()
    assert not _ready(runtime, cfg)


def test_semantic_off_accepts_only_current_bound_cloud_contract(runtime):
    led, _, _ = runtime
    cfg = {'semantic': {'mode': 'off'}}
    seed(led, 1, proof=False)
    _raw_proof(led, 1, 1, cloud=True)
    assert _ready(runtime, cfg)
    led.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.prompt','old') WHERE kind='extract_llm'")
    led.db.commit()
    assert not _ready(runtime, cfg)


def test_semantic_off_rejects_exhausted_failure_and_missing_post(runtime):
    led, _, _ = runtime
    cfg = {'semantic': {'mode': 'off'}}
    seed(led, 1, proof=False)
    digest = led.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
    led.artifact_add('extract_llm', '{}', project_id=1, message_id=1, model='synthetic',
                     meta={VERSION_KEY: CURRENT_VERSION, 'hash': digest,
                           'extract_version': extract_llm.EXTRACT_VERSION, 'error': 1, 'attempts': 5})
    assert not _ready(runtime, cfg)
    _raw_proof(led, 1, 1)
    assert _ready(runtime, cfg)
    seed(led, 2, state='missing', body='', proof=False)
    assert not _ready(runtime, cfg)


def test_all_posts_need_current_evidence_and_complete_acquisition(runtime):
    led, _, _ = runtime
    cfg = _cfg("assist")
    seed(led, 1, proof=False)
    seed(led, 2, proof=False)
    _proof(led, 1, 1, cfg)
    assert not _ready(runtime, cfg)
    _proof(led, 1, 2, cfg)
    assert _ready(runtime, cfg)
    led.db.execute("UPDATE patients SET history_floor=0 WHERE project_id=1")
    led.db.commit()
    assert not _ready(runtime, cfg)


@pytest.mark.parametrize("state,body", [("deleted", ""), ("full", ""), ("missing", "")])
def test_unacquired_empty_deleted_or_unavailable_post_cannot_be_complete(runtime, state, body):
    led, _, _ = runtime
    cfg = _cfg("assist")
    seed(led, 1, proof=False)
    _proof(led, 1, 1, cfg)
    seed(led, 2, body=body, state=state, proof=False)
    with led.db:
        led.db.execute("UPDATE patients SET fetch_state='pending',history_floor=NULL WHERE project_id=1")
    assert not _ready(runtime, cfg)


def test_old_generation_and_intake_hold_block(runtime):
    led, _, _ = runtime
    cfg = _cfg("assist")
    seed(led, 1, proof=False)
    _proof(led, 1, 1, cfg)
    assert _ready(runtime, cfg)
    led.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.inference_engine_version','0.1') "
                   "WHERE kind='semantic_audit' AND message_id=1")
    led.db.commit()
    assert not _ready(runtime, cfg)
    _proof(led, 1, 1, cfg)
    assert _ready(runtime, cfg)
    led.artifact_add("semantic_intake_hold_v1", "{}", project_id=1, model="1")
    assert not _ready(runtime, cfg)


def test_pending_or_failed_job_and_new_arrival_block(runtime):
    led, _, _ = runtime
    cfg = _cfg("assist")
    seed(led, 1, proof=False)
    _proof(led, 1, 1, cfg)
    assert _ready(runtime, cfg)
    for state in ("pending", "failed"):
        led.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,state,created_at,updated_at) "
                       "VALUES('semantic',1,1,?,0,0)", (state,))
        led.db.commit()
        assert not _ready(runtime, cfg)
        led.db.execute("DELETE FROM fetch_jobs WHERE kind='semantic'")
        led.db.commit()
    seed(led, 2, proof=False)
    assert not _ready(runtime, cfg)


def test_qc_must_match_current_extraction(runtime):
    led, clock, _ = runtime
    cfg = _cfg("assist", extract_qc="annotate")
    seed(led, 1, proof=False)
    led.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=1", (clock.wall,))
    led.db.commit()
    _proof(led, 1, 1, cfg)
    digest = led.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
    meta = {VERSION_KEY: CURRENT_VERSION, "hash": digest, "extract_version": extract_llm.EXTRACT_VERSION}
    source_id = led.artifact_add("extract_llm", "{}", project_id=1, message_id=1, meta=meta)
    assert not _ready(runtime, cfg)
    led.artifact_add("extract_qc", "{}", project_id=1, message_id=1,
                     meta={**meta, "source_artifact_id": source_id})
    assert _ready(runtime, cfg)
    led.artifact_add("extract_llm", "{}", project_id=1, message_id=1, meta=meta)
    assert not _ready(runtime, cfg)


def test_latest_ready_active_before_archive(runtime, monkeypatch):
    led, _, _ = runtime
    for pid in (1, 2, 3, 4):
        seed(led, pid, pid, proof=pid != 3)
        led.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=?", (pid, pid))
    led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=4")
    led.db.commit()
    monkeypatch.setattr(extract_llm, "_writer_current", lambda **_kw: True)
    result = step(runtime)
    assert result["status"] == "progressed"
    assert refresh._latest(led.db, refresh.PROGRESS, 2)[0]
    assert not refresh._latest(led.db, refresh.PROGRESS, 4)[0]


def test_readiness_change_before_reservation_prevents_writes(runtime, monkeypatch):
    led, _, calls = runtime
    seed(led)
    results = iter((True, False))
    monkeypatch.setattr(refresh, "_inference_ready", lambda *_a, **_kw: next(results))
    monkeypatch.setattr(extract_llm, "_writer_current", lambda **_kw: True)
    before = led.db.total_changes
    assert step(runtime)["status"] == "source_changed"
    assert led.db.total_changes == before
    assert not calls


def test_new_post_during_inference_prevents_adoption(runtime, monkeypatch):
    led, _, calls = runtime
    seed(led)
    monkeypatch.setattr(extract_llm, "_writer_current", lambda **_kw: True)
    original = refresh.local_llm.admitted_chat

    def interrupted(*args, **kwargs):
        answer = original(*args, **kwargs)
        seed(led, 2, proof=False)
        return answer

    monkeypatch.setattr(refresh.local_llm, "admitted_chat", interrupted)
    assert step(runtime)["status"] == "source_changed"
    assert calls
    assert refresh._latest(led.db, refresh.KIND, 1)[0] == {}


def test_retained_karte_may_be_scanned_but_not_used_for_summary_before_audit(runtime):
    led, _, _ = runtime
    seed(led)
    with led.db:
        led.db.execute('UPDATE patients SET karte_id=17 WHERE project_id=1')
    led.karte_summary_store(1, 17, {'comment': '合成の旧資料。', 'updated_at': '2026-10-15T09:00:00+09:00'})
    led.karte_summary_store(1, 17, {'comment': '合成の新資料。', 'updated_at': '2026-10-16T09:00:00+09:00'})
    from test_patient_context_refresh import CFG
    snapshot = refresh._snapshot(led.db, 1, CFG, hydrate=False)
    assert snapshot['dependencies']['coverage']['reasons'].get('retained_karte_requires_audit')
    assert not _ready(runtime, CFG)
    assert _ready(runtime, CFG, allow_dependency_repair=True)
    result = step(runtime)
    assert result['status'] == 'progressed' and result['lane'] in ('B', 'C')
    assert refresh._latest(led.db, refresh.KIND, 1)[0] == {}



def _classification_worker(runtime, *, initial_summary='off'):
    led, _, _ = runtime
    cfg = _cfg('assist', summary_mode=initial_summary, loop_mode='off')
    seed(led, 1, proof=False)
    _raw_proof(led, 1, 1)
    led.semantic_seed(1, [1], {'source': 'synthetic-history'})
    out = semantic_drain.run_due(led, cfg, {'errors': []}, time.monotonic() + 300,
                                 jev_client=_FakeJev(), llm_fn=_llm)
    assert out['done'] == 1
    assert led.artifacts('semantic_assess', message_id=1)
    return _cfg('assist', summary_mode='off', loop_mode='off')


def test_classification_only_accepts_real_current_assessment(runtime):
    led, _, _ = runtime
    cfg = _classification_worker(runtime)
    assert not led.artifacts('semantic_summary', message_id=1)
    assert _ready(runtime, cfg)


@pytest.mark.parametrize('damage', ['pending', 'failed', 'raw_missing', 'raw_corrupt',
                                    'assessment_corrupt', 'target', 'policy', 'partial', 'verdict',
                                    'negative_audit', 'source_changed', 'loop_enabled', 'detail_disguise',
                                    'fact_pass_negative', 'skip_negative', 'audit_unbound'])
def test_classification_only_holds_unproven_evidence(runtime, damage):
    led, _, _ = runtime
    cfg = _classification_worker(runtime)
    assert _ready(runtime, cfg)
    row = led.db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind='semantic_assess' "
                         "AND project_id=1 AND message_id=1 ORDER BY artifact_id DESC LIMIT 1").fetchone()
    if damage in ('pending', 'failed'):
        led.db.execute("UPDATE fetch_jobs SET state=? WHERE kind='semantic'", (damage,))
    elif damage == 'raw_missing':
        led.db.execute("DELETE FROM artifacts WHERE kind='extract_llm'")
    elif damage == 'raw_corrupt':
        led.db.execute("UPDATE artifacts SET meta='{}' WHERE kind='extract_llm'")
    elif damage == 'assessment_corrupt':
        led.db.execute("UPDATE artifacts SET meta='{\"x\":1,\"x\":2}' WHERE artifact_id=?", (row['artifact_id'],))
    elif damage == 'target':
        led.db.execute("UPDATE artifacts SET content=json_set(content,'$.target_message_id',2) WHERE artifact_id=?", (row['artifact_id'],))
    elif damage == 'policy':
        led.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.policy_fingerprint','other') WHERE artifact_id=?", (row['artifact_id'],))
    elif damage == 'partial':
        led.db.execute("UPDATE artifacts SET content=json_set(content,'$.verdicts',json('{}')) WHERE artifact_id=?", (row['artifact_id'],))
    elif damage == 'verdict':
        content = json.loads(row['content'])
        first = next(iter(content['verdicts']))
        content['verdicts'][first]['verdict'] = 'synthetic-invalid'
        led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(content), row['artifact_id']))
    elif damage == 'negative_audit':
        led.artifact_add('semantic_audit', json.dumps({'status': 'NEEDS_REVIEW'}),
                         project_id=1, message_id=1, meta=json.loads(row['meta']))
    elif damage in ('fact_pass_negative', 'skip_negative', 'audit_unbound'):
        kind = 'semantic_facts_audit' if damage == 'fact_pass_negative' else 'semantic_audit'
        status = 'PASS' if damage != 'skip_negative' else 'SKIPPED'
        code = 'fact_contradicts' if damage == 'fact_pass_negative' else 'claim_not_supported'
        meta = json.loads(row['meta']) if damage != 'audit_unbound' else {VERSION_KEY: CURRENT_VERSION}
        led.artifact_add(kind, json.dumps({'status': status, 'findings': [{'code': code}]}),
                         project_id=1, message_id=1, meta=meta)
    elif damage == 'detail_disguise':
        led.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.fact_id','synthetic') WHERE artifact_id=?",
                       (row['artifact_id'],))
    elif damage == 'source_changed':
        led.db.execute("UPDATE messages SET body_text='synthetic changed',content_hash='changed' WHERE message_id=1")
    else:
        cfg = _cfg('assist', summary_mode='off', loop_mode='assist')
    led.db.commit()
    assert not _ready(runtime, cfg)


def test_classification_only_ignores_newer_detail_assessment(runtime):
    led, _, _ = runtime
    cfg = _classification_worker(runtime)
    import semantic_assessment
    bundle = semantic_store.thread_bundle(led.db, 1, 1, [1])
    policy = semantic_policy.runtime_config(cfg)[0]
    semantic_assessment._persist_detail(led, 1, 1, bundle['source_fingerprint'],
        semantic_policy.policy_fingerprint(policy), 'synthetic-fact', 'status', 'planned', 0.9)
    assert _ready(runtime, cfg)


def test_classification_only_accepts_real_disabled_jev_skip(runtime):
    led, _, _ = runtime
    cfg = _cfg('assist', summary_mode='off', loop_mode='off', daily_request_budget=0)
    seed(led, 1, proof=False)
    _raw_proof(led, 1, 1)
    led.semantic_seed(1, [1], {'source': 'synthetic-history'})
    out = semantic_drain.run_due(led, cfg, {'errors': []}, time.monotonic() + 300,
                                 jev_client=None, llm_fn=_llm)
    assert out['done'] == 1
    row = led.artifacts('semantic_assess', message_id=1)[-1]
    assert json.loads(row['meta'])['technical_status'] == 'skipped'
    policy = semantic_policy.runtime_config(cfg)[0]
    source = led.db.execute("SELECT m.*, p.project_id AS patient_id, p.fetch_reason FROM messages m "
                            "JOIN patients p ON p.project_id=m.project_id WHERE m.message_id=1").fetchone()
    assert semantic_drain._reprocess_target_state(led, source, policy, cfg=cfg) == 'current_skipped'
    assert _ready(runtime, cfg)


def test_classification_only_reuses_current_assessment_from_summary_mode(runtime):
    led, _, _ = runtime
    cfg_on = _cfg('assist', summary_mode='assist', loop_mode='off')
    cfg_off = _cfg('assist', summary_mode='off', loop_mode='off')
    on, errors = semantic_policy.runtime_config(cfg_on)
    off, errors_off = semantic_policy.runtime_config(cfg_off)
    assert not errors and not errors_off
    assert semantic_policy.policy_fingerprint(on) == semantic_policy.policy_fingerprint(off)
    seed(led, 1, proof=False)
    _raw_proof(led, 1, 1)
    bundle = semantic_store.thread_bundle(led.db, 1, 1, [1])
    result, flag = semantic_drain._assess_target(
        led, on, bundle, _FakeJev(), 1, 1, bundle['source_fingerprint'],
        semantic_policy.policy_fingerprint(on), None, time.monotonic() + 60, diagnostics={})
    assert result and flag is None
    assert _ready(runtime, cfg_off)


def test_classification_skip_preserves_previous_partial_verdict(runtime):
    led, _, _ = runtime
    cfg = _cfg('assist', summary_mode='off', loop_mode='off', daily_request_budget=0)
    policy, errors = semantic_policy.runtime_config(cfg)
    assert not errors
    seed(led, 1, proof=False)
    _raw_proof(led, 1, 1)
    bundle = semantic_store.thread_bundle(led.db, 1, 1, [1])
    name = next(iter(semantic_drain.jev.PROPOSITIONS))
    verdict = {'noul': 0.1, 'verdict': semantic_drain.jev.verdict_for(
        0.1, policy['match_threshold'], policy['nomatch_threshold'])}
    led.artifact_add('semantic_assess', json.dumps({'target_message_id': 1, 'verdicts': {name: verdict}}),
        project_id=1, message_id=1, model=semantic_drain.jev.JEV_MODEL,
        meta={VERSION_KEY: CURRENT_VERSION, 'fingerprint': bundle['source_fingerprint'],
              'policy_fingerprint': semantic_policy.policy_fingerprint(policy),
              'schema': semantic_drain.SCHEMA_VERSION, 'technical_status': 'retry_wait'})
    led.semantic_seed(1, [1], {'source': 'synthetic-history'})
    out = semantic_drain.run_due(led, cfg, {'errors': []}, time.monotonic() + 300,
                                 jev_client=None, llm_fn=_llm)
    assert out['done'] == 1 and _ready(runtime, cfg)
    latest = led.artifacts('semantic_assess', message_id=1)[-1]
    assert json.loads(latest['content'])['verdicts'] == {name: verdict}
    led.db.execute("UPDATE artifacts SET content=json_set(content,'$.verdicts',json('{}')) WHERE artifact_id=?",
                   (latest['artifact_id'],))
    led.db.commit()
    assert not _ready(runtime, cfg)


def test_classification_rejects_forged_no_signal_marker(runtime):
    led, _, _ = runtime
    cfg = _cfg('assist', summary_mode='off', loop_mode='off')
    seed(led, 1, body='完全合成の症状の記録。', proof=False)
    _raw_proof(led, 1, 1)
    with led.db:
        led.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.prefilter','no_signal') WHERE kind='extract_llm'")
    led.semantic_seed(1, [1], {'source': 'synthetic-history'})
    out = semantic_drain.run_due(led, cfg, {'errors': []}, time.monotonic() + 300,
                                 jev_client=_FakeJev(), llm_fn=_llm)
    assert out['done'] == 1
    assert json.loads(led.artifacts('semantic_assess', message_id=1)[-1]['meta'])['skipped'] == 'prefilter_no_signal'
    assert not _ready(runtime, cfg)


@pytest.mark.parametrize('old_value,expected', [(0.9, True), (0.1, False)])
def test_complete_assessment_cannot_discard_prior_partial(runtime, old_value, expected):
    led, _, _ = runtime
    cfg = _cfg('assist', summary_mode='off', loop_mode='off')
    policy, errors = semantic_policy.runtime_config(cfg)
    assert not errors
    seed(led, 1, proof=False)
    _raw_proof(led, 1, 1)
    bundle = semantic_store.thread_bundle(led.db, 1, 1, [1])
    name = next(iter(semantic_drain.jev.PROPOSITIONS))
    verdict = {'noul': old_value, 'verdict': semantic_drain.jev.verdict_for(
        old_value, policy['match_threshold'], policy['nomatch_threshold'])}
    led.artifact_add('semantic_assess', json.dumps({'target_message_id': 1, 'verdicts': {name: verdict}}),
        project_id=1, message_id=1, model=semantic_drain.jev.JEV_MODEL,
        meta={VERSION_KEY: CURRENT_VERSION, 'fingerprint': bundle['source_fingerprint'],
              'policy_fingerprint': semantic_policy.policy_fingerprint(policy),
              'schema': semantic_drain.SCHEMA_VERSION, 'technical_status': 'retry_wait'})
    led.semantic_seed(1, [1], {'source': 'synthetic-history'})
    out = semantic_drain.run_due(led, cfg, {'errors': []}, time.monotonic() + 300,
                                 jev_client=_FakeJev(), llm_fn=_llm)
    assert out['done'] == 1
    assert _ready(runtime, cfg) is expected


def test_classification_only_requires_current_qc_for_reprocess(runtime):
    led, clock, _ = runtime
    cfg = _cfg('assist', summary_mode='off', loop_mode='off', extract_qc='annotate')
    seed(led, 1, proof=False)
    with led.db:
        led.db.execute('UPDATE messages SET posted_at_ts=? WHERE message_id=1', (clock.wall,))
    _raw_proof(led, 1, 1)
    led.semantic_seed(1, [1], {'source': 'synthetic-history'})
    out = semantic_drain.run_due(led, cfg, {'errors': []}, time.monotonic() + 300,
                                 jev_client=_FakeJev(), llm_fn=_llm)
    assert out['done'] >= 1
    row = led.db.execute("SELECT m.*,p.project_id AS patient_id,p.fetch_reason FROM messages m "
                         "JOIN patients p ON p.project_id=m.project_id WHERE m.message_id=1").fetchone()
    policy = semantic_policy.runtime_config(cfg)[0]
    raw = led.db.execute("SELECT artifact_id FROM artifacts WHERE kind='extract_llm' AND message_id=1 "
                         "ORDER BY artifact_id DESC LIMIT 1").fetchone()[0]
    meta = {VERSION_KEY: CURRENT_VERSION, 'hash': row['content_hash'],
            'extract_version': extract_llm.EXTRACT_VERSION, 'source_artifact_id': raw}
    led.artifact_add('extract_qc', '{}', project_id=1, message_id=1, meta=meta)
    led.db.execute("DELETE FROM fetch_jobs WHERE kind='extract_qc'")
    led.db.commit()
    assert semantic_drain._reprocess_target_state(led, row, policy, cfg=cfg) == 'current'
    led.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,state,created_at,updated_at) "
                   "VALUES('extract_qc',1,1,'pending',0,0)")
    led.db.commit()
    assert semantic_drain._reprocess_target_state(led, row, policy, cfg=cfg) == 'held'
    led.db.execute("DELETE FROM fetch_jobs WHERE kind='extract_qc'")
    led.db.execute("DELETE FROM artifacts WHERE kind='extract_qc'")
    led.db.commit()
    assert semantic_drain._reprocess_target_state(led, row, policy, cfg=cfg) == 'held'


@pytest.mark.parametrize('summary_mode', ['shadow', 'assist', 'enforce'])
def test_classification_only_reuses_real_summary_on_assessment_and_pass(runtime, summary_mode):
    led, _, _ = runtime
    cfg_on = _cfg('assist', summary_mode=summary_mode, loop_mode='off')
    cfg_off = _cfg('assist', summary_mode='off', loop_mode='off')
    policy = semantic_policy.runtime_config(cfg_on)[0]
    seed(led, 1, proof=False)
    _raw_proof(led, 1, 1)
    _semantic_proof(led, 1, 1, cfg_on)
    bundle = semantic_store.thread_bundle(led.db, 1, 1, [1])
    verdicts, flag = semantic_drain._assess_target(
        led, policy, bundle, _FakeJev(), 1, 1, bundle['source_fingerprint'],
        semantic_policy.policy_fingerprint(policy), None, time.monotonic() + 60, diagnostics={})
    assert verdicts and flag is None
    assert _ready(runtime, cfg_off)


@pytest.mark.parametrize('damage', ['audit_status', 'audit_hash', 'summary_hash', 'binding'])
def test_classification_only_holds_corrupt_summary_audit_pair(runtime, damage):
    led, _, _ = runtime
    cfg_on = _cfg('assist', summary_mode='assist', loop_mode='off')
    cfg_off = _cfg('assist', summary_mode='off', loop_mode='off')
    policy = semantic_policy.runtime_config(cfg_on)[0]
    seed(led, 1, proof=False)
    _raw_proof(led, 1, 1)
    _semantic_proof(led, 1, 1, cfg_on)
    bundle = semantic_store.thread_bundle(led.db, 1, 1, [1])
    result, flag = semantic_drain._assess_target(
        led, policy, bundle, _FakeJev(), 1, 1, bundle['source_fingerprint'],
        semantic_policy.policy_fingerprint(policy), None, time.monotonic() + 60, diagnostics={})
    assert result and flag is None and _ready(runtime, cfg_off)
    fields = {'audit_status': '$.audit_status', 'audit_hash': '$.audit_content_hash',
              'summary_hash': '$.summary_hash', 'binding': '$.claim_input_binding'}
    led.db.execute("UPDATE artifacts SET meta=json_set(meta,?,?) WHERE kind='semantic_audit'",
                   (fields[damage], 'NEEDS_REVIEW' if damage == 'audit_status' else 'corrupt'))
    led.db.commit()
    assert not _ready(runtime, cfg_off)
