"""A model support score cannot approve an unverified quantity conversion."""
import semantic_drain
import semantic_policy
import semantic_store
import json
import time

import pytest

from semantic_testkit import _FakeJev, _cfg, _llm, _seeded
from mcs_util import payload_hash


def _latest_bound_audits(db):
    latest = {}
    for row in sorted(db.artifacts('semantic_audit'), key=lambda row: row['artifact_id']):
        latest[row['message_id']] = row
    bundle = semantic_store.thread_bundle(db.db, 1, 1)
    members = {member['message_id']: member for member in bundle['members']}
    policy = semantic_policy.policy_fingerprint(semantic_policy.semantic_config(_cfg('shadow'))[0])
    for row in latest.values():
        meta = json.loads(row['meta'])
        summary = db.db.execute('SELECT project_id,message_id,content,meta FROM artifacts WHERE artifact_id=?',
                                (meta['summary_artifact_id'],)).fetchone()
        assert summary is not None
        summary_meta = json.loads(summary['meta'])
        assert summary['project_id'] == row['project_id'] and summary['message_id'] == row['message_id']
        assert meta['fingerprint'] == summary_meta['fingerprint'] == bundle['source_fingerprint']
        assert meta['policy_fingerprint'] == summary_meta['policy_fingerprint'] == policy
        assert meta['target_revision'] == summary_meta['target_revision'] == members[row['message_id']]['revision']
        assert meta['summary_hash'] == payload_hash(json.loads(summary['content']))
        assert meta['audit_content_hash'] == payload_hash(json.loads(row['content']))
        assert meta['audit_status'] == summary_meta['audit_status']
    return list(latest.values())


@pytest.mark.parametrize('replacement', ['300g', '300μg', '300mg/mL', '500mg', '300mg/kg', '300mg/5mL', '-300mg', '1,300mg', '300mg/日'])
def test_summary_quantity_change_cannot_pass_on_model_support(tmp_path, replacement):
    db = _seeded(tmp_path)
    def llm(prompt):
        response = _llm(prompt)
        if response and '要約器' in prompt:
            return response.replace('300mg', replacement)
        return response
    try:
        semantic_drain.run_due(db, _cfg('shadow'), {'errors': []},
                         time.monotonic() + 300,
                         jev_client=_FakeJev(), llm_fn=llm)
        audits = _latest_bound_audits(db)
        assert audits
        assert all(json.loads(row['meta'])['audit_status'] == 'NEEDS_REVIEW'
                   for row in audits)
        pending = [json.loads(row['content']) for row in db.artifacts('semantic_audit')
                   if json.loads(row['meta'])['audit_status'] == 'PENDING']
        assert len(pending) == len(audits)
        assert all(any(finding['code'].startswith('claim_quantity_')
                       for finding in audit['findings']) for audit in pending)
        assert len(db.artifacts('semantic_repair')) == len(audits)
        assert not db.db.execute(
            "SELECT 1 FROM notify_outbox WHERE kind='semantic_notice'").fetchone()
    finally:
        db.close()


@pytest.mark.parametrize('omit_quantity', [False, True])
def test_repaired_quantity_preserves_normal_summary_path(tmp_path, omit_quantity):
    db = _seeded(tmp_path)
    summaries = 0
    def llm(prompt):
        nonlocal summaries
        response = _llm(prompt)
        if response and '要約器' in prompt:
            summaries += 1
            if summaries == 1:
                return response.replace('300mg', '300g')
            if omit_quantity:
                value = json.loads(response)
                for claim in value['claims']:
                    claim['text'] = claim['text'].replace('300mg×3回', '新しい用量')
                return json.dumps(value)
        return response
    try:
        semantic_drain.run_due(db, _cfg('shadow'), {'errors': []},
                         time.monotonic() + 300,
                         jev_client=_FakeJev(), llm_fn=llm)
        audits = [json.loads(row['meta']) for row in _latest_bound_audits(db)]
        expected = 'NEEDS_REVIEW' if omit_quantity else 'PASS'
        assert audits and all(row['audit_status'] == expected for row in audits)
        pending = [json.loads(row['content']) for row in db.artifacts('semantic_audit')
                   if json.loads(row['meta'])['audit_status'] == 'PENDING']
        repairs = db.artifacts('semantic_repair')
        assert len(pending) == len(repairs) == sum(row['repair_count'] for row in audits)
        assert all(audit['findings'] for audit in pending)
        if not omit_quantity:
            assert len(repairs) == 1
    finally:
        db.close()
