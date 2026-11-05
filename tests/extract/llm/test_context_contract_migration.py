"""Known pre-fix contracts preserve synthetic immutable inputs and paid accounting."""
import extract_llm_context
import extract_llm_prepare
import extract_llm_runner
from contextlib import nullcontext
from dataclasses import replace
import json
import time

import pytest

import anthropic_client
import cloud_backlog
import cloud_policy
import extract_llm as worker
import extraction_context as frames
import extraction_medications
from extract_testkit import _message
from inference_dependency_loss import source_binding
from inference_version import CURRENT_VERSION, VERSION_KEY
from test_anthropic_runtime import old, routed, real_usage_reserver, real_record_result

__all__ = ['routed']
# git show 710b1356:mcs/extract/llm/extraction_context.py | shasum -a 256
PRE_FIX_SOURCE = '08a818e1843d577cca760be8db70ccb385803c3624bc6d92b70dc56672595088'


def frozen(ledger, digest):
    old(ledger)
    row = dict(ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    row.update(_source_binding=source_binding(ledger.db, 1, 1), _target=('', 'fictional-model'))
    hints = (extraction_medications.build(ledger.db, {}, row['body_text'], private=True,
             deadline=time.monotonic() + 10) if digest == PRE_FIX_SOURCE else None)
    attempt = {'qc_src': None, 'thin_src': None, 'human_src': None, 'auto_retry': 0}
    frame = frames.prepare(ledger, row, model='fictional-model', contract=worker._context_contract(source_digest=digest),
        attempt=attempt, enabled=False, base_context=None, version=worker.EXTRACT_VERSION, medication_hints=hints)
    return row, frame, hints, attempt


@pytest.mark.parametrize('digest', [worker._PRIOR_CONTEXT_SOURCE, PRE_FIX_SOURCE])
def test_known_contract_pending_chunk_resumes_unchanged(routed, digest):
    ledger, *_ = routed
    row, frame, _hints, attempt = frozen(ledger, digest)
    aid = ledger.artifact_add('extract_llm_chunk', '{"symptoms":[]}', project_id=1, message_id=1,
        model='fictional-model', meta={'hash': row['content_hash'], 'ver': worker.EXTRACT_VERSION,
            VERSION_KEY: CURRENT_VERSION, 'context': worker._chunk_context(
                {**row, '_frame': frame}, frame['text'], checkpoint=True)})
    before = [dict(r) for r in ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id IN (?,?)', (frame['artifact_id'], aid))]
    picked = worker._pending_context_frame(ledger.db, row, attempt=attempt, enabled=False)
    assert picked == frame
    assert [dict(r) for r in ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id IN (?,?)', (frame['artifact_id'], aid))] == before


@pytest.mark.parametrize('changed_source', [False, True])
@pytest.mark.parametrize('digest', [worker._PRIOR_CONTEXT_SOURCE, PRE_FIX_SOURCE])
def test_known_contract_exact_input_reuse_or_proven_old_occurrence(routed, digest, changed_source):
    ledger, *_ = routed
    row, first, hints, attempt = frozen(ledger, digest)
    before = dict(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (first['artifact_id'],)).fetchone())
    if changed_source:
        ledger.save_messages([replace(_message(body=row['body_text']), sender_name='合成の別の投稿者')])
        row = dict(ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
        row['_source_binding'] = source_binding(ledger.db, 1, 1)
    current = worker._context_contract()
    after = frames.prepare(ledger, row, model='fictional-model', contract=current,
        compatible_contracts=tuple(worker._context_contracts() - {current}),
        attempt=attempt, enabled=False, base_context=None, version=worker.EXTRACT_VERSION, medication_hints=hints)
    assert (after['artifact_id'] != first['artifact_id']) is changed_source
    assert dict(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (first['artifact_id'],)).fetchone()) == before


@pytest.mark.parametrize('outcome', ['paid', 'unknown'])
def test_pre_fix_frame_keeps_paid_recovery_or_unknown_hold_at_next_cap(routed, monkeypatch, outcome):
    ledger, cfg, _fake_calls, _reserves, _results = routed
    old(ledger)
    monkeypatch.setattr(cloud_policy, 'usage_reserver', real_usage_reserver)
    monkeypatch.setattr(cloud_policy, 'record_result', real_record_result)
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *a, **kw: True)
    monkeypatch.setattr(cloud_backlog, 'owner_alive', lambda: True)
    monkeypatch.setattr(cloud_backlog, 'request_slot', lambda *a: nullcontext(True))
    client = anthropic_client.AnthropicClient.__bases__[0]
    posts, replay = [], []
    def request(self, method, body, timeout, **kw):
        posts.append(body)
        return 200, {}, json.dumps({'model': anthropic_client.MODEL, 'stop_reason': 'end_turn',
            'content': [{'type': 'text', 'text': '{"symptoms":[]}'}],
            'usage': {'input_tokens': 10, 'output_tokens': 2}}).encode()
    monkeypatch.setattr(client, 'request', request)
    monkeypatch.setattr(anthropic_client, 'AnthropicClient', client)
    original_contract = worker._context_contract
    monkeypatch.setattr(worker, '_context_contract', lambda source_digest=None:
                        original_contract(source_digest=source_digest or PRE_FIX_SOURCE))
    monkeypatch.setattr(extract_llm_context, '_context_contract', worker._context_contract)
    monkeypatch.setattr(extract_llm_prepare, '_context_contract', worker._context_contract)
    first_pass = [True]
    def extract(body, **kwargs):
        provider = kwargs.get('provider')
        assert provider is not None, 'a prior unknown must not fall back locally'
        if first_pass[0] and outcome == 'unknown':
            provider.reserve(provider.client.body('synthetic migration prompt', 100, None), 30)
        else:
            answer = provider('synthetic migration prompt', deadline=kwargs['deadline'], max_tokens=100)
            replay.append(bool(answer.get('reused_response')))
        return worker._DEFERRED
    monkeypatch.setattr(worker, 'llm_extract', extract)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    assert worker.run_pending(ledger, limit=1, budget_s=180)['deferred'] == 1
    before = [dict(r) for r in ledger.db.execute("SELECT * FROM artifacts WHERE kind IN "
        "('extract_llm_context_v1','cloud_usage_reserve','cloud_usage_result') ORDER BY artifact_id")]
    first_pass[0] = False
    monkeypatch.setattr(worker, '_context_contract', original_contract)
    monkeypatch.setattr(extract_llm_context, '_context_contract', worker._context_contract)
    monkeypatch.setattr(extract_llm_prepare, '_context_contract', worker._context_contract)
    cfg['cloud_llm']['daily_cost_limit_usd'] = 0
    assert worker.run_pending(ledger, limit=1, budget_s=180)['deferred'] == 1
    assert len(posts) == int(outcome == 'paid')
    assert replay == ([False, True] if outcome == 'paid' else [])
    assert [dict(r) for r in ledger.db.execute("SELECT * FROM artifacts WHERE kind IN "
        "('extract_llm_context_v1','cloud_usage_reserve','cloud_usage_result') ORDER BY artifact_id")] == before
