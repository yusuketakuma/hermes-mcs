"""A refused fresh checkpoint never loses or resends the already paid answer."""
import extract_llm_runner
from contextlib import nullcontext
import json

import cloud_policy
import extract_llm as worker
from test_anthropic_runtime import AnthropicClient, _use_real_cloud_accounting, old, routed as routed


def test_refused_fresh_checkpoint_reuses_saved_paid_answer_after_finite_wait(routed, monkeypatch):
    ledger, cfg, _calls, _reserves, _results = routed
    old(ledger)
    _use_real_cloud_accounting(monkeypatch)
    monkeypatch.setattr(worker.cloud_backlog, 'request_slot', lambda *args: nullcontext(True))
    monkeypatch.setattr(worker.cloud_backlog, 'owner_alive', lambda: True)
    posts = []
    monkeypatch.setattr(worker, '_llm_up', lambda **kwargs: False)
    monkeypatch.setattr(extract_llm_runner, '_llm_up', worker._llm_up)
    def request(self, method, body, timeout, **kwargs):
        posts.append(body)
        return 200, {}, json.dumps({'model': worker.anthropic_client.MODEL, 'stop_reason': 'end_turn',
            'content': [{'type': 'text', 'text': worker._PREFILTER_CONTENT}],
            'usage': {'input_tokens': 10, 'output_tokens': 5}}).encode()
    monkeypatch.setattr(AnthropicClient, 'request', request)
    persist = worker._persist_chunks
    refused = []
    def once(*args, **kwargs):
        if not refused:
            refused.append(True)
            return False
        return persist(*args, **kwargs)
    monkeypatch.setattr(worker, '_persist_chunks', once)
    monkeypatch.setattr(extract_llm_runner, '_persist_chunks', worker._persist_chunks)
    first = worker.run_pending(ledger, limit=1, budget_s=180)
    assert len(refused) == len(posts) == 1
    assert first['done'] == first['failed'] == 0 and first['deferred'] == 1
    assert first['defer_reasons'] == {'checkpoint_unverified': 1}
    assert not ledger.artifacts('extract_llm_chunk')
    money = [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts WHERE kind IN "
        "('cloud_usage_reserve','cloud_usage_result','cloud_usage_disposition') ORDER BY artifact_id")]
    assert len(ledger.artifacts('cloud_usage_reserve')) == len(ledger.artifacts('cloud_usage_result')) == 1
    wait = ledger.db.execute("SELECT next_try FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0]
    assert worker.time.time() < wait <= worker.time.time() + worker._PREPARE_HOLD_S
    # Selection wait expires, while accounting/cache and guard checks remain real.
    now = worker.time.time()
    monkeypatch.setattr(worker.time, 'time', lambda: now + worker._PREPARE_HOLD_S + 1)
    cfg['cloud_llm']['daily_cost_limit_usd'] = 0
    second = worker.run_pending(ledger, limit=1, budget_s=180)
    assert second['done'] == 1 and second['failed'] == second['deferred'] == 0
    assert len(posts) == 1
    assert [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts WHERE kind IN "
        "('cloud_usage_reserve','cloud_usage_result','cloud_usage_disposition') ORDER BY artifact_id")] == money
    assert not ledger.artifacts('extract_llm_chunk')
    assert not ledger.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='extract_claim'").fetchone()
    assert float(cloud_policy.usage_report(ledger)['max_reserved_cost_usd']) == 0
