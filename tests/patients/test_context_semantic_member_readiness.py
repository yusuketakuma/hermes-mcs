"""Current member proofs may progress while another thread member waits."""
import json
import time

import pytest

from extract_testkit import _message
import patient_context_quota as quota
import patient_context_refresh_readiness as readiness
import patient_context_refresh_sources as sources
import patient_context_refresh_state as state
import semantic_drain
import semantic_policy
import semantic_runtime
import semantic_store
from mcs_util import payload_hash
from semantic_testkit import _PassJev, _cfg, _summary_from_facts
from test_patient_context_refresh import CFG, _raw_proof, _semantic_proof, document, runtime, seed, step

__all__ = ['runtime']


def _row(led, mid=1):
    return led.db.execute('SELECT m.*,p.project_id AS patient_id,p.fetch_reason '
        'FROM messages m JOIN patients p ON p.project_id=m.project_id '
        'WHERE m.message_id=?', (mid,)).fetchone()


def _status(led, cfg, mid=1):
    policy, errors = semantic_policy.runtime_config(cfg)
    assert not errors
    return semantic_store.reprocess_target_state(led, _row(led, mid), policy, cfg=cfg)


def _partial_thread(runtime, *, fact_source='legacy'):
    led, _, _ = runtime
    cfg = _cfg('assist', summary_mode='shadow', loop_mode='off', fact_source=fact_source)
    cfg['extract'] = CFG['extract']
    seed(led, body='生活環境: 合成の居住記録。', proof=False)
    led.save_messages([_message(2, '生活環境: 合成の別の記録。', parent_id=1)])
    for mid in (1, 2):
        _raw_proof(led, 1, mid)
    led.semantic_seed(1, [1, 2], {'source': 'synthetic-history', 'notification_free': True})
    summaries = []

    def local(prompt):
        if '要約器' in prompt:
            summaries.append(prompt)
            if len(summaries) > 1:
                raise semantic_runtime.LLMNotSent('local_slot_busy')
            return _summary_from_facts(prompt)
        quote = prompt.split('<<<\n', 1)[1].split('\n>>>', 1)[0]
        return json.dumps({'facts': [{'statement': quote, 'evidence_quote': quote,
            'kind': 'observation', 'status': 'not_stated', 'polarity': 'affirmed'}]})

    result = semantic_drain.run_due(led, cfg, {'errors': []}, time.monotonic() + 300,
                                   jev_client=_PassJev(), llm_fn=local)
    assert len(summaries) == 2 and result['done'] == 0, result
    job = led.db.execute("SELECT state FROM fetch_jobs WHERE kind=? AND message_id=1",
                         (semantic_policy.JOB_KIND,)).fetchone()
    assert job is not None and job['state'] != 'done'
    # The real worker's partial-publish branch wrote this target's bound pair.
    assert _status(led, cfg) == 'current'
    assert _status(led, cfg, 2) not in ('current', 'current_skipped')
    return cfg


def _keys(runtime, cfg):
    led, clock, _ = runtime
    snapshot = sources._snapshot(led.db, 1, cfg, hydrate=False)
    return readiness._eligible_source_keys(led, 1, cfg, snapshot, deadline=clock.mono + 600)


@pytest.mark.parametrize('fact_source', ['legacy', 'shadow'])
def test_current_member_scans_while_other_thread_member_waits(runtime, fact_source):
    led, clock, calls = runtime
    cfg = _partial_thread(runtime, fact_source=fact_source)
    assert _keys(runtime, cfg) == (1,)
    assert readiness._inference_ready(led, 1, cfg, deadline=clock.mono + 600, source_keys=[1])
    assert not readiness._inference_ready(led, 1, cfg, deadline=clock.mono + 600)
    before = len(calls)
    for lane in ('B', 'C'):
        result = step(runtime, cfg=cfg)
        assert result['status'] == 'progressed' and result['lane'] == lane and result['done'] == 1, result
    assert len(calls) == before + 1
    scan = document(led, state.SCAN)
    assert scan['complete'] and scan['audited']
    result = step(runtime, cfg=cfg)
    assert result['status'] == 'progressed' and result['lane'] == 'A' and result['done'] == 1, result
    assert len(calls) == before + 2 and sum('reports' in call for call in calls) == 1
    view = readiness.context_view_input(led.db, 1, cfg=cfg)
    assert view is not None and view['summary']
    assert view['facts'] and {fact['message_id'] for fact in view['facts']} == {1}
    assert view['coverage']['partial'] is True and view['coverage']['unreflected'] == 1
    assert readiness.inference_coverage(led.db, 1, cfg)['complete'] is False
    _, meta = state._latest(led.db, state.KIND, 1)
    assert meta['source_scope'] == [1]
    assert led.db.execute('SELECT COUNT(*) FROM artifacts WHERE kind=?', (quota.KIND,)).fetchone()[0] == 1
    assert not led.artifacts(state.SCAN, project_id=1, message_id=2)
    assert _status(led, cfg, 2) not in ('current', 'current_skipped')
    assert not led.artifacts('cloud_usage_reserve')


@pytest.mark.parametrize('damage', ['source_edit', 'new_reply', 'audit_failed', 'audit_unknown', 'receipt'])
def test_root_wait_does_not_release_stale_or_unverified_member(runtime, damage):
    led, clock, calls = runtime
    cfg = _partial_thread(runtime)
    if damage == 'source_edit':
        led.save_messages([_message(1, '生活環境: 合成の更新後の記録。')])
    elif damage == 'new_reply':
        led.save_messages([_message(3, '生活環境: 合成の新着返信。', parent_id=1)])
    else:
        audit = led.artifacts(semantic_policy.KIND_AUDIT, project_id=1, message_id=1)[-1]
        meta = json.loads(audit['meta'])
        if damage == 'receipt':
            meta.pop('claim_input_binding')
            with led.db:
                led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?',
                               (json.dumps(meta), audit['artifact_id']))
        else:
            status = 'NEEDS_REVIEW' if damage == 'audit_failed' else 'PENDING'
            summary = json.loads(led.artifacts(semantic_policy.KIND_SUMMARY, message_id=1)[-1]['content'])
            summary['audit_status'] = status
            bundle = semantic_store.thread_bundle(led.db, 1, 1, [1])
            result = {'summary': summary, 'findings': [{'code': 'support_contradicted'
                if damage == 'audit_failed' else 'support_unevaluated'}], 'repaired': False,
                'claim_input_binding': meta['claim_input_binding']}
            with led.db:
                semantic_drain._write_result(led, 1, 1, result, bundle['source_fingerprint'],
                    {m['message_id']: m for m in bundle['members']}, status,
                    meta['policy_fingerprint'], cfg['semantic']['summary_mode'])
            newest = led.artifacts(semantic_policy.KIND_AUDIT, message_id=1)[-1]
            # Refusal must be the real adverse/unknown outcome, not a broken hash.
            assert json.loads(newest['meta'])['audit_content_hash'] == payload_hash(json.loads(newest['content']))
    assert _status(led, cfg) not in ('current', 'current_skipped')
    assert not _keys(runtime, cfg)
    assert not readiness._inference_ready(led, 1, cfg, deadline=clock.mono + 600, source_keys=[1])
    result = step(runtime, cfg=cfg)
    assert result['status'] == 'deferred' and result['done'] == 0
    assert not calls and not led.artifacts('cloud_usage_reserve')


@pytest.mark.parametrize('loop_mode', ['off', 'shadow'])
def test_summary_off_keeps_root_completion_gate(runtime, loop_mode):
    led, clock, _ = runtime
    cfg = _partial_thread(runtime)
    cfg['semantic'].update(summary_mode='off', loop_mode=loop_mode)
    policy, errors = semantic_policy.runtime_config(cfg)
    assert not errors
    assert not readiness._message_ready(led, _row(led), cfg, policy,
                                       deadline=clock.mono + 600, attachment_mode='on')


@pytest.mark.parametrize('body_state', ['full', 'deleted'])
def test_empty_or_deleted_member_keeps_root_completion_gate(runtime, body_state):
    led, clock, _ = runtime
    seed(led, body='', state=body_state, proof=False)
    led.semantic_seed(1, [1], {'source': 'synthetic-history', 'notification_free': True})
    policy, errors = semantic_policy.runtime_config(CFG)
    assert not errors
    assert led.db.execute("SELECT 1 FROM fetch_jobs WHERE kind=? AND state!='done'",
                          (semantic_policy.JOB_KIND,)).fetchone()
    assert not readiness._message_ready(led, _row(led), CFG, policy,
                                       deadline=clock.mono + 600, attachment_mode='on')


def test_canonical_member_cannot_use_legacy_facts_as_its_proof(runtime):
    led, clock, _ = runtime
    cfg = _cfg('enforce', summary_mode='assist', loop_mode='off', fact_source='canonical')
    seed(led, proof=False)
    _raw_proof(led, 1, 1)
    _semantic_proof(led, 1, 1, cfg=cfg)
    led.semantic_seed(1, [1], {'source': 'synthetic-history', 'notification_free': True})
    assert led.artifacts(semantic_policy.KIND_FACTS, message_id=1)
    assert not led.artifacts(semantic_policy.KIND_FACTS_V2, message_id=1)
    assert _status(led, cfg) == 'held'
    policy, errors = semantic_policy.runtime_config(cfg)
    assert not errors
    assert not readiness._message_ready(led, _row(led), cfg, policy,
                                       deadline=clock.mono + 600, attachment_mode='on')
