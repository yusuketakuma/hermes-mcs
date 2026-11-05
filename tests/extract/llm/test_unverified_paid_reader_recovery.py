"""Known paid answers with removed uncertainty keys survive fresh replay and strict reading."""
import extract_llm_runner
from contextlib import nullcontext
import json

import extract_llm as worker
from extract_testkit import _hash, _message
from test_anthropic_runtime import AnthropicClient, _use_real_cloud_accounting, routed as routed


def test_zero_http_paid_replay_writes_unverified_candidate_without_reintroducing_false(routed, monkeypatch):
    ledger, cfg, _calls, _reserves, _results = routed
    body = '本人は合成薬Aを中止したかもしれません。'
    ledger.save_messages([_message(body=body)])
    ledger.artifact_add(worker.KIND, '{}', project_id=1, message_id=1,
        meta={'hash': _hash(ledger), 'extract_version': worker.EXTRACT_VERSION - 1})
    _use_real_cloud_accounting(monkeypatch)
    monkeypatch.setattr(worker.cloud_backlog, 'request_slot', lambda *args: nullcontext(True))
    monkeypatch.setattr(worker.cloud_backlog, 'owner_alive', lambda: True)
    monkeypatch.setattr(worker, '_llm_up', lambda **kwargs: False)
    monkeypatch.setattr(extract_llm_runner, '_llm_up', worker._llm_up)
    answer = {'symptoms': [], 'meds': [{'name': '合成薬A', 'dose': None, 'status': 'current',
        'subject': 'patient', 'action': 'stop', 'negated': False, 'evidence': body}]}
    posts = []
    def request(self, method, payload, timeout, **kwargs):
        posts.append(payload)
        return 200, {}, json.dumps({'model': worker.anthropic_client.MODEL, 'stop_reason': 'end_turn',
            'content': [{'type': 'text', 'text': json.dumps(answer, ensure_ascii=False)}],
            'usage': {'input_tokens': 10, 'output_tokens': 5}}).encode()
    monkeypatch.setattr(AnthropicClient, 'request', request)
    atomic = worker._commit_extraction_atomic
    reached = []
    def formerly_rejected(_ledger, row, *args, **kwargs):
        reached.append(True)
        # Seed the old rejected-save state, keeping all paid answers/checkpoints.
        return worker._commit_rejected(row, 'completeness_pending')
    monkeypatch.setattr(worker, '_commit_extraction_atomic', formerly_rejected)
    monkeypatch.setattr(extract_llm_runner, '_commit_extraction_atomic', worker._commit_extraction_atomic)
    first = worker.run_pending(ledger, limit=1, budget_s=180)
    assert reached == [True] and first['done'] == first['failed'] == 0 and first['deferred'] == 1
    assert first['defer_reasons'] == {'commit_completeness_pending': 1}
    assert posts
    money = [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts WHERE kind IN "
        "('cloud_usage_reserve','cloud_usage_result','cloud_usage_disposition') ORDER BY artifact_id")]
    assert len(ledger.artifacts('cloud_usage_reserve')) == len(ledger.artifacts('cloud_usage_result')) == len(posts)
    assert not ledger.artifacts('cloud_usage_disposition')
    monkeypatch.setattr(worker, '_commit_extraction_atomic', atomic)
    monkeypatch.setattr(extract_llm_runner, '_commit_extraction_atomic', worker._commit_extraction_atomic)
    capture = worker._capture_checkpoint_state
    fresh_replays = []
    def without_cached_leaf(db, row, context):
        saved = capture(db, row, context)
        assert saved and saved[0]['meds'][0]['unverified'] is True
        assert 'negated' not in saved[0]['meds'][0]
        fresh_replays.append(True)
        # Model the old reader's missing value at selection only. Keep its exact
        # source/layout/identity and all rows; the corrected strict reader checks
        # the newly persisted candidate again at the actual atomic commit.
        return {}
    monkeypatch.setattr(worker, '_capture_checkpoint_state', without_cached_leaf)
    monkeypatch.setattr(extract_llm_runner, '_capture_checkpoint_state', worker._capture_checkpoint_state)
    now = worker.time.time()
    monkeypatch.setattr(worker.time, 'time', lambda: now + worker._PREPARE_HOLD_S + 1)
    cfg['cloud_llm']['daily_cost_limit_usd'] = 0
    count = len(posts)
    second = worker.run_pending(ledger, limit=1, budget_s=180)
    assert fresh_replays == [True]
    assert second['done'] == 1 and second['failed'] == second['deferred'] == 0
    assert second['llm_calls']['calls'] == 0 and len(posts) == count
    assert [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts WHERE kind IN "
        "('cloud_usage_reserve','cloud_usage_result','cloud_usage_disposition') ORDER BY artifact_id")] == money
    current = ledger.db.execute("SELECT content FROM artifacts WHERE kind='extract_llm' ORDER BY artifact_id DESC LIMIT 1").fetchone()
    final = json.loads(current[0])
    assert final['meds'][0]['unverified'] is True and 'negated' not in final['meds'][0]
    assert final['_evidence_dropped'] > 0
