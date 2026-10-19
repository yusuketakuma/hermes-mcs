"""Relation retrieval failures remain explicit without becoming clinical relations."""
import semantic_policy
import semantic_store
import json
import time

import pytest

import ledger
import mcs_view
import semantic_jev as jev
import semantic_loops
from semantic_testkit import _cfg, _pending_fact, _seeded


def _setup(tmp_path):
    db = _seeded(tmp_path)
    bundle = semantic_store.thread_bundle(db.db, 1, 1, [2])
    config, _ = semantic_policy.semantic_config(_cfg())
    policy = semantic_policy.policy_fingerprint(config)
    db.artifact_add('semantic_policy', policy)
    semantic_loops.update_loops(db, 1, bundle,
                               {1: _pending_fact(bundle['members'][0])},
                               None, config, time.monotonic() + 30)
    candidate = db.artifacts('loop_candidate', message_id=1)[-1]
    trigger = next(m for m in bundle['members'] if m['message_id'] == 2)
    receipt = {'status': 'INCOMPLETE', 'loop_artifact_id': candidate['artifact_id'],
               'trigger_message_id': 2, 'trigger_revision': trigger['revision'],
               'code': 'relation:probabilities_sum_off_ge_1e-3'}
    meta = {'fingerprint': bundle['source_fingerprint'], 'policy_fingerprint': policy,
            'registry': jev.REGISTRY_VERSION, 'retry_scope': 'synthetic-old-approval'}
    return db, bundle, candidate, receipt, meta


def _view(db, tmp_path):
    snapshot = ledger.publish_snapshot(str(tmp_path / 'ledger.db'), str(tmp_path / 'snap'))
    view = mcs_view.View(snapshot)
    try:
        return view.read('loops', project=1)['items'][0]
    finally:
        view.close()


def test_current_rejection_is_visible_without_promoting_the_candidate(tmp_path):
    db, _, _, receipt, meta = _setup(tmp_path)
    try:
        for _ in range(2):
            db.artifact_add('loop_relation_rejected', json.dumps(receipt),
                            project_id=1, message_id=2, meta=meta)
        item = _view(db, tmp_path)
        assert item['relation_evaluation_unavailable'] == 1
        assert item['relation_evaluation_notice'] == '後続の投稿1件について関係評価を取得できませんでした'
        assert item['relation_events'] == []
        assert item['effective_state'] == 'PROPOSED'
        assert db.db.execute('SELECT count(*) FROM requests').fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize('change', ['status', 'code', 'id_bool', 'revision', 'registry', 'scope', 'invalidated', 'fp', 'policy'])
def test_corrupt_or_stale_rejection_is_retained_but_not_counted(tmp_path, change):
    db, _, _, receipt, meta = _setup(tmp_path)
    try:
        if change == 'status':
            receipt['status'] = 'unknown'
        elif change == 'code':
            receipt['code'] = 'SYNTHETIC_PRIVATE_RESPONSE'
        elif change == 'id_bool':
            receipt['trigger_message_id'] = True
        elif change == 'revision':
            receipt['trigger_revision'] = 'old-revision'
        elif change == 'registry':
            meta['registry'] = 'old-registry'
        elif change == 'scope':
            meta['retry_scope'] = None
        elif change == 'invalidated':
            meta['invalidated'] = True
        elif change == 'fp':
            meta['fingerprint'] = 'old-fingerprint'
        else:
            meta['policy_fingerprint'] = 'old-policy'
        db.artifact_add('loop_relation_rejected', json.dumps(receipt),
                        project_id=1, message_id=2, meta=meta)
        item = _view(db, tmp_path)
        assert item['relation_evaluation_unavailable'] == 0
        assert item['relation_evaluation_notice'] is None
        assert 'SYNTHETIC_PRIVATE_RESPONSE' not in repr(item)
        assert len(db.artifacts('loop_relation_rejected', message_id=2)) == 1
    finally:
        db.close()


@pytest.mark.parametrize('relation', ['unrelated', 'unclear', 'completion_report'])
def test_a_valid_current_event_supersedes_the_old_rejection(tmp_path, relation):
    db, bundle, candidate, receipt, meta = _setup(tmp_path)
    try:
        db.artifact_add('loop_relation_rejected', json.dumps(receipt),
                        project_id=1, message_id=2, meta=meta)
        event = {'loop_artifact_id': candidate['artifact_id'], 'loop_origin_id': 1,
                 'origin_revision': bundle['members'][0]['revision'],
                 'trigger_message_id': 2, 'trigger_revision': receipt['trigger_revision'],
                 'relation': relation, 'reported_relation': relation, 'confidence': 0.9}
        db.artifact_add('loop_event', json.dumps(event), project_id=1, message_id=2, meta=meta)
        item = _view(db, tmp_path)
        assert item['relation_evaluation_unavailable'] == 0
        assert len(db.artifacts('loop_relation_rejected', message_id=2)) == 1
    finally:
        db.close()


@pytest.mark.parametrize('fault', ['old_revision', 'confidence_bool', 'confidence_missing',
                                 'relation_object', 'reported_relation_array'])
def test_stale_or_invalid_event_does_not_hide_retrieval_failure(tmp_path, fault):
    db, bundle, candidate, receipt, meta = _setup(tmp_path)
    try:
        db.artifact_add('loop_relation_rejected', json.dumps(receipt),
                        project_id=1, message_id=2, meta=meta)
        event = {'loop_artifact_id': candidate['artifact_id'], 'loop_origin_id': 1,
                 'origin_revision': bundle['members'][0]['revision'],
                 'trigger_message_id': 2, 'trigger_revision': receipt['trigger_revision'],
                 'relation': 'unrelated', 'reported_relation': 'unrelated', 'confidence': 0.9}
        if fault == 'old_revision':
            event['trigger_revision'] = 'old-revision'
        elif fault == 'confidence_bool':
            event['confidence'] = True
        elif fault == 'confidence_missing':
            event.pop('confidence')
        elif fault == 'relation_object':
            event['relation'] = {}
        else:
            event['reported_relation'] = []
        db.artifact_add('loop_event', json.dumps(event), project_id=1, message_id=2, meta=meta)
        assert _view(db, tmp_path)['relation_evaluation_unavailable'] == 1
    finally:
        db.close()
