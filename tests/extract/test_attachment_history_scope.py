"""OCR cloud sends retain the existing human history approval and accounting boundaries."""
import json
import time

import pytest

import attachment_inference as worker
import cloud_policy
from test_attachment_inference import page_store, run, routed, local_then_cloud

__all__ = ['page_store', 'routed', 'local_then_cloud']


@pytest.mark.parametrize('mutation', ['missing_scope', 'excluded_parent', 'recent_parent', 'recent_notification'])
def test_cloud_requires_receipted_parent_history_and_age(page_store, mutation):
    ledger, cfg, key, _, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    with ledger.db:
        if mutation == 'missing_scope':
            ledger.db.execute("DELETE FROM artifacts WHERE kind='cloud_history_scope'")
        elif mutation == 'excluded_parent':
            ledger.artifact_add_tx('cloud_history_scope', '{"version":1,"sources":{}}')
        elif mutation == 'recent_parent':
            ledger.db.execute('UPDATE messages SET first_seen=? WHERE message_id=1', (time.time(),))
        else:
            ledger.db.execute('UPDATE messages SET notified_at=? WHERE message_id=1', (time.time(),))
    result = run(ledger, cfg, 'cloud')
    assert result['done'] == 0 and result['deferred'] > 0
    assert not cloud_calls and not ledger.artifacts('cloud_usage_reserve')
    assert worker.current(ledger.db, 1, key, cfg=cfg)['ready'] is False


def test_scope_revoked_after_unit_creation_stops_send(page_store, monkeypatch):
    ledger, cfg, _, _, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    make_unit = worker.cloud_inference.CloudInferenceUnit
    def revoked(*args, **kwargs):
        unit = make_unit(*args, **kwargs)
        with ledger.db:
            ledger.artifact_add_tx('cloud_history_scope', '{"version":1,"sources":{}}')
        return unit
    monkeypatch.setattr(worker.cloud_inference, 'CloudInferenceUnit', revoked)
    result = run(ledger, cfg, 'cloud')
    assert result['done'] == 0 and result['deferred'] > 0
    assert not cloud_calls


@pytest.mark.parametrize('mutation,adopt', [('scope', False), ('notification', True)])
def test_received_paid_response_checks_scope_but_not_new_send_age(page_store, monkeypatch, mutation, adopt):
    ledger, cfg, key, _, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    client = worker.extract_llm.anthropic_client.AnthropicClient
    request = client.request
    def changed(self, *args, **kwargs):
        answer = request(self, *args, **kwargs)
        with ledger.db:
            if mutation == 'scope':
                ledger.artifact_add_tx('cloud_history_scope', '{"version":1,"sources":{}}')
            else:
                ledger.db.execute('UPDATE messages SET notified_at=? WHERE message_id=1', (time.time(),))
        return answer
    monkeypatch.setattr(client, 'request', changed)
    result = run(ledger, cfg, 'cloud')
    assert len(cloud_calls) == 1 and ledger.artifacts('cloud_usage_reserve')
    assert ledger.artifacts('cloud_usage_result')
    assert (result['done'] == 1) is adopt
    assert worker.current(ledger.db, 1, key, cfg=cfg)['ready'] is adopt
    if not adopt:
        assert run(ledger, cfg, 'cloud')['done'] == 0 and len(cloud_calls) == 1


def test_cloud_input_binding_is_independent_of_approval_generation(page_store):
    ledger, cfg, key, *_ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    source = worker.original_source_keys.source(ledger.db, 1, key)
    before = worker._history_snapshot(ledger.db, source, require_ready=True)
    assert before
    local = worker.current(ledger.db, 1, key, cfg=cfg)
    first_binding = worker._input_binding(source, local)
    with ledger.db:
        ledger.db.execute('BEGIN IMMEDIATE')
        cloud_policy.capture_history_tx(ledger.db, 'synthetic-new-approved-scope', time.time())
    after = worker._history_snapshot(ledger.db, source, require_ready=True)
    assert after and after != before
    assert worker._input_binding(source, local) == first_binding


def test_parent_body_edit_during_http_holds_then_readopts_same_paid_answer(page_store, monkeypatch):
    from dataclasses import replace
    from extract_testkit import _message
    ledger, cfg, key, _, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    source_before = worker.original_source_keys.binding(ledger.db, 1, key)
    client = worker.extract_llm.anthropic_client.AnthropicClient
    request = client.request
    def edited(self, *args, **kwargs):
        answer = request(self, *args, **kwargs)
        # This text is not in the OCR prompt; only its independent history approval changes.
        ledger.save_messages([replace(_message(body='合成の親投稿を更新しました。'), message_id=1)])
        return answer
    monkeypatch.setattr(client, 'request', edited)
    assert run(ledger, cfg, 'cloud')['done'] == 0
    assert len(cloud_calls) == 1
    assert worker.original_source_keys.binding(ledger.db, 1, key) == source_before
    assert ledger.artifacts('cloud_usage_result') and not ledger.artifacts('cloud_usage_disposition')
    assert worker.current(ledger.db, 1, key, cfg=cfg)['ready'] is False
    # Reapprove the changed parent immediately: received answers do not need a new-send age window.
    with ledger.db:
        ledger.db.execute('BEGIN IMMEDIATE')
        cloud_policy.capture_history_tx(ledger.db, 'synthetic-reapproved-parent', time.time(), continuous=True)
        meta = {'command_id': 'synthetic-reapproved-parent', 'actor': 'synthetic', 'reason': 'synthetic'}
        ledger.artifact_add_tx('cloud_control', 'running', meta=meta)
        receipt = {**meta, 'outcome': 'applied', 'control': 'running'}
        ledger.db.execute('INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)',
            ('synthetic-reapproved-parent', 'synthetic-hash', None, None, 'applied', json.dumps(receipt), time.time()))
    assert worker._history_snapshot(ledger.db, worker.original_source_keys.source(ledger.db, 1, key))
    assert run(ledger, cfg, 'cloud')['done'] == 1
    assert worker.current(ledger.db, 1, key, cfg=cfg)['ready'] is True
    assert len(cloud_calls) == len(ledger.artifacts('cloud_usage_reserve')) == 1
