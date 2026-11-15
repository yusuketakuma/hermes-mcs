"""Synthetic local completion is final: the individual cloud lane stays closed."""
import extract_llm_provider
import extract_llm_runner
from contextlib import nullcontext
import json
import time

import pytest

import cloud_policy
import extract_llm as worker
from extract_testkit import _message
from test_anthropic_runtime import AnthropicClient, _current_artifacts, _use_real_cloud_accounting, routed

__all__ = ['routed']


@pytest.fixture
def local_then_cloud(routed, monkeypatch):
    ledger, cfg, _fixture_calls, _fixture_reserves, _fixture_results = routed
    cfg['cloud_llm']['mode'] = 'off'
    cfg['cloud_llm']['max_concurrency'] = 1
    ledger.save_messages([_message(body='完全合成の経過記録です。')])
    with ledger.db:
        ledger.db.execute('UPDATE messages SET first_seen=?', (time.time() - 1200,))
        ledger.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 1200,))
        cloud_policy.capture_history_tx(ledger.db, 'synthetic-dual-completion', time.time())
    local_calls, cloud_calls = [], []
    answers = {'local': {'symptoms': []}, 'cloud': {'symptoms': []}}
    def local_chat(prompt, **kwargs):
        local_calls.append(prompt)
        return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps(answers['local'], ensure_ascii=False)}
    monkeypatch.setattr(worker, '_probe_format', lambda **kwargs: 'plain')
    monkeypatch.setattr(extract_llm_provider, '_probe_format', worker._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', worker._probe_format)
    monkeypatch.setattr(worker, '_llm_up', lambda **kwargs: True)
    monkeypatch.setattr(extract_llm_runner, '_llm_up', worker._llm_up)
    monkeypatch.setattr(worker.local_llm, 'chat', local_chat)
    monkeypatch.setattr(worker.local_llm, 'admitted_chat', lambda _client, prompt, **kwargs: local_chat(prompt, **kwargs))
    monkeypatch.setattr(worker.cloud_backlog, 'owner_alive', lambda: True)
    monkeypatch.setattr(worker.cloud_backlog, 'request_slot', lambda *a: nullcontext(True))
    _use_real_cloud_accounting(monkeypatch)
    def request(self, method, body, timeout, **kwargs):
        cloud_calls.append(body)
        return 200, {}, json.dumps({'model': worker.anthropic_client.MODEL,
            'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps(answers['cloud'], ensure_ascii=False)}],
            'usage': {'input_tokens': 10, 'output_tokens': 2}}).encode()
    monkeypatch.setattr(AnthropicClient, 'request', request)
    yield ledger, cfg, local_calls, cloud_calls, answers


def _local_pass(ledger):
    return worker.run_pending(ledger, limit=1, workers=1, budget_s=180, allow_cloud=False)


def _current(ledger):
    rows = _current_artifacts(ledger)
    assert len(rows) == 1
    return rows[0], json.loads(rows[0]['meta'])


def _financial_rows(ledger):
    return [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result',"
        "'cloud_usage_disposition') ORDER BY artifact_id")]


def _stale_prior(ledger, artifact_id):
    """Make a current local artifact stale so the next pass owes this row work."""
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.extract_version',?) "
                          "WHERE artifact_id=?", (worker.EXTRACT_VERSION - 1, artifact_id))


def test_stale_prior_is_reprocessed_locally_without_cloud_send(local_then_cloud, monkeypatch):
    ledger, cfg, local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    local, _meta = _current(ledger)
    cfg['cloud_llm']['mode'] = 'on'
    _stale_prior(ledger, local['artifact_id'])
    monkeypatch.setattr(worker.cloud_backlog, 'owner_alive', lambda: False)
    report = worker.run_pending(ledger, limit=1, workers=1, budget_s=180)
    assert report['done'] == 1, 'closed cloud lane leaves stale work to the local lane'
    assert len(local_calls) == 2 and not cloud_calls
    assert not _financial_rows(ledger), 'nothing was sent or billed'
    current, meta = _current(ledger)
    assert current['artifact_id'] != local['artifact_id']
    assert meta['provider_provenance']['providers'] == ['local']


def _selected_contract(cfg):
    selection, errors = cloud_policy.resolve_profile(cfg, route='reextract', purpose='backlog')
    assert not errors
    return worker.cloud_inference.cloud_provider_contract(selection=selection)


def _seal_canonical_v4(ledger, mid=1):
    """Seal the source with a current v4 projection, as semantic adoption does."""
    from semantic_projection import PROJECTION_VERSION
    import inference_version
    source = ledger.db.execute(
        "SELECT content_hash, project_id, message_id FROM messages WHERE message_id=?",
        (mid,)).fetchone()
    meta = {"hash": source["content_hash"], "engine_version": 4,
            "projection_version": PROJECTION_VERSION,
            inference_version.VERSION_KEY: inference_version.CURRENT_VERSION}
    cursor = ledger.db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at)"
        " VALUES('semantic_facts_v4',?,?,'{}','synthetic-v4',?,?)",
        (source["project_id"], source["message_id"], json.dumps(meta), time.time()))
    ledger.db.commit()
    return cursor.lastrowid


def _v4_row(ledger, artifact_id):
    return tuple(ledger.db.execute(
        "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone())


