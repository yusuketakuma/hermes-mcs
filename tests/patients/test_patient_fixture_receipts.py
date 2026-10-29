"""Synthetic patient fixtures obey the current semantic receipt contract."""
import json

import pytest

import semantic_drain
import semantic_policy
import semantic_send_gate
import semantic_store
from inference_version import CURRENT_VERSION, VERSION_KEY
from test_patient_context_refresh import CFG, seed, runtime as runtime


def _current(led, pid=1, mid=1):
    policy, errors = semantic_policy.runtime_config(CFG)
    assert not errors
    bundle = semantic_store.thread_bundle(led.db, pid, mid, [mid])
    member = bundle['members'][0]
    return semantic_send_gate.passed_summary_current(
        led.db, pid, mid, bundle['source_fingerprint'],
        semantic_policy.policy_fingerprint(policy), member['revision'],
        policy['summary_mode'], bundle=bundle, fact_source=policy['fact_source'])


def _held(led):
    policy, errors = semantic_policy.runtime_config(CFG)
    assert not errors
    row = led.db.execute('SELECT m.*,p.project_id AS patient_id,p.fetch_reason '
                         'FROM messages m LEFT JOIN patients p ON p.project_id=m.project_id '
                         'WHERE m.project_id=1 AND m.message_id=1').fetchone()
    return semantic_drain._reprocess_target_state(led, row, policy) == 'held'


def test_patient_fixture_writes_source_bound_receipts_for_each_project(runtime):
    led, _, _ = runtime
    for pid, mid in ((1, 1), (2, 2)):
        seed(led, pid=pid, mid=mid, body=f'完全合成の生活記録その{pid}。')
        policy = semantic_policy.runtime_config(CFG)[0]
        bundle = semantic_store.thread_bundle(led.db, pid, mid, [mid])
        policy_fp = semantic_policy.policy_fingerprint(policy)
        pair = semantic_send_gate._paired_summary(led.db, pid, mid,
            bundle['source_fingerprint'], policy_fp, bundle['members'][0]['revision'],
            policy['summary_mode'], 'PASS')
        assert pair is not None, 'writer pair must be current'
        assert semantic_send_gate._claim_binding_current(led.db, pid, mid,
            bundle['source_fingerprint'], policy_fp, bundle['members'][0]['revision'],
            *pair, bundle['members'][0], policy['fact_source']), 'claim binding must be current'
        assert _current(led, pid, mid)
        fp = semantic_store.thread_bundle(led.db, pid, mid, [mid])['source_fingerprint']
        facts = semantic_store.current_artifact(led.db, semantic_drain.KIND_FACTS, mid, fp)
        fact = facts['content']['facts'][0]
        assert fact['statement'] == fact['_evidence']['quote']
        assert fact['_evidence']['message_id'] == mid


@pytest.mark.parametrize('defect', ['summary_artifact_id', 'summary_hash',
                                  'audit_content_hash', 'claim_input_binding'])
def test_patient_fixture_rejects_unbound_receipt(runtime, defect):
    led, _, _ = runtime
    seed(led)
    assert _current(led)
    row = led.db.execute('SELECT artifact_id,meta FROM artifacts WHERE kind=? '
                         'ORDER BY artifact_id DESC LIMIT 1',
                         (semantic_drain.KIND_AUDIT,)).fetchone()
    meta = json.loads(row['meta'])
    meta.pop(defect)
    with led.db:
        led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?',
                       (json.dumps(meta), row['artifact_id']))
    assert not _current(led)
    assert _held(led)


def test_old_patient_fixture_standalone_pass_is_held(runtime):
    led, _, _ = runtime
    seed(led, proof=False)
    policy, _ = semantic_policy.runtime_config(CFG)
    bundle = semantic_store.thread_bundle(led.db, 1, 1, [1])
    meta = {VERSION_KEY: CURRENT_VERSION, 'fingerprint': bundle['source_fingerprint'],
            'policy_fingerprint': semantic_policy.policy_fingerprint(policy)}
    for kind, content in ((semantic_drain.KIND_FACTS, {}),
                          (semantic_drain.KIND_SUMMARY, {}),
                          (semantic_drain.KIND_AUDIT, {'status': 'PASS'})):
        led.artifact_add(kind, json.dumps(content), project_id=1, message_id=1, meta=meta)
    assert not _current(led)
    assert _held(led)


def test_patient_fixture_empty_claims_remain_held_even_with_writer_pair(runtime):
    led, _, _ = runtime
    seed(led)
    bundle = semantic_store.thread_bundle(led.db, 1, 1, [1])
    member = bundle['members'][0]
    policy, _ = semantic_policy.runtime_config(CFG)
    policy_fp = semantic_policy.policy_fingerprint(policy)
    facts = semantic_store.current_artifact(
        led.db, semantic_drain.KIND_FACTS, 1, bundle['source_fingerprint'])['content']['facts']
    summary = {'target_message_id': 1, 'claims': [], 'audit_status': 'PASS'}
    result = {'summary': summary, 'findings': [], 'repaired': False,
              'claim_input_binding': semantic_drain._claim_input_binding(
                  1, 1, bundle['source_fingerprint'], policy_fp, member['revision'], summary, facts)}
    with led.db:
        semantic_drain._write_result(led, 1, 1, result, bundle['source_fingerprint'],
            {1: member}, 'PASS', policy_fp, policy['summary_mode'])
    assert not _current(led)
    assert _held(led)
