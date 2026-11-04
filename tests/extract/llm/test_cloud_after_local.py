"""Synthetic local-first completion must leave a source eligible for paid cloud extraction."""
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
    monkeypatch.setattr(worker, '_llm_up', lambda **kwargs: True)
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


def _cloud_pass(ledger):
    return worker.run_pending(ledger, limit=1, workers=1, budget_s=180,
                              cloud_only=True, background=True)


def _current(ledger):
    rows = _current_artifacts(ledger)
    assert len(rows) == 1
    return rows[0], json.loads(rows[0]['meta'])


def test_real_local_completion_is_followed_by_one_cloud_extraction(local_then_cloud):
    ledger, cfg, local_calls, cloud_calls, _answers = local_then_cloud
    first = _local_pass(ledger)
    assert first['done'] == 1 and local_calls and not cloud_calls
    local, meta = _current(ledger)
    assert meta['provider_provenance']['providers'] == ['local']
    cfg['cloud_llm']['mode'] = 'on'
    second = _cloud_pass(ledger)
    assert second['done'] == 1, 'a current local result must not satisfy cloud completion'
    cloud, meta = _current(ledger)
    assert cloud['artifact_id'] != local['artifact_id']
    assert meta['backend'] == 'anthropic' and meta['provider_provenance']['providers'] == ['anthropic']
    assert len(cloud_calls) == len(ledger.artifacts('cloud_usage_reserve')) == len(ledger.artifacts('cloud_usage_result')) == 1
    third = _cloud_pass(ledger)
    assert third['done'] == 0 and len(cloud_calls) == 1


def _financial_rows(ledger):
    return [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result',"
        "'cloud_usage_disposition') ORDER BY artifact_id")]


@pytest.mark.parametrize('boundary', ['claim', 'publish'])
def test_local_commit_race_cannot_cancel_required_cloud_pass(local_then_cloud, monkeypatch, boundary):
    ledger, cfg, local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    local, _meta = _current(ledger)
    local = dict(local)
    # Replay the exact record from the real local execution at the raced commit
    # boundary. No candidate metadata or execution/provenance proof is invented.
    with ledger.db:
        ledger.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (local['artifact_id'],))
    cfg['cloud_llm']['mode'] = 'on'
    reached = []
    target = '_claim' if boundary == 'claim' else '_commit_extraction_atomic'
    original = getattr(worker, target)
    def local_finishes(db, row, *args, **kwargs):
        if not reached:
            columns = tuple(local)
            with db.db:
                db.db.execute('INSERT INTO artifacts (' + ','.join(columns) + ') VALUES ('
                              + ','.join('?' for _ in columns) + ')', tuple(local.values()))
            reached.append(row['message_id'])
        return original(db, row, *args, **kwargs)
    monkeypatch.setattr(worker, target, local_finishes)
    report = _cloud_pass(ledger)
    assert reached == [1] and report['done'] == 1
    cloud, meta = _current(ledger)
    assert cloud['artifact_id'] != local['artifact_id']
    assert meta['provider_provenance']['providers'] == ['anthropic']
    assert len(cloud_calls) == 1 and len(local_calls) == 1
    financial = _financial_rows(ledger)
    assert _cloud_pass(ledger)['done'] == 0
    assert len(cloud_calls) == 1 and _financial_rows(ledger) == financial


def test_protected_local_prior_is_held_without_cloud_completion_or_resend(local_then_cloud):
    ledger, cfg, _local_calls, cloud_calls, answers = local_then_cloud
    body = '本人は合成薬Aを服用していません。'
    ledger.save_messages([_message(body=body)])
    with ledger.db:
        ledger.db.execute('UPDATE messages SET first_seen=?', (time.time() - 1200,))
        ledger.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 1200,))
        cloud_policy.capture_history_tx(ledger.db, 'synthetic-protected-prior', time.time())
    answers['local'] = {'symptoms': [], 'meds': [{'name': '合成薬A', 'dose': None,
        'status': 'current', 'subject': 'patient', 'action': 'none', 'negated': True, 'evidence': body}]}
    assert _local_pass(ledger)['done'] == 1
    local, _meta = _current(ledger)
    assert json.loads(local['content'])['meds'][0]['name'] == '合成薬A'
    assert json.loads(local['content'])['meds'][0]['negated'] is True
    cfg['cloud_llm']['mode'] = 'on'
    report = _cloud_pass(ledger)
    assert report['done'] == 0 and report['held_prior'] == 1
    assert cloud_calls, 'the hold must follow actual cloud extraction, not pre-send exclusion'
    current, meta = _current(ledger)
    assert current['artifact_id'] == local['artifact_id'] and current['content'] == local['content']
    assert meta['provider_provenance']['providers'] == ['local']
    selection, errors = cloud_policy.resolve_profile(cfg, route='reextract', purpose='backlog')
    assert not errors
    source = ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()
    assert worker.progress_for_message(ledger.db, source, model=selection['model'])['state'] != 'complete'
    hold = [json.loads(row['content']) for row in ledger.artifacts(worker.UPGRADE_KIND)]
    assert any(item.get('reason') == 'protected_loss' for item in hold)
    financial, count = _financial_rows(ledger), len(cloud_calls)
    assert _cloud_pass(ledger)['done'] == 0
    assert _financial_rows(ledger) == financial and len(cloud_calls) == count


def test_unknown_paid_cloud_keeps_local_prior_and_never_resends(local_then_cloud, monkeypatch):
    ledger, cfg, local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    local, _meta = _current(ledger)
    cfg['cloud_llm']['mode'] = 'on'
    reached = []
    def interrupted(self, method, body, timeout, **kwargs):
        reached.append(body)
        raise KeyboardInterrupt('synthetic interruption after reservation')
    monkeypatch.setattr(AnthropicClient, 'request', interrupted)
    with pytest.raises(KeyboardInterrupt):
        _cloud_pass(ledger)
    assert len(reached) == len(ledger.artifacts('cloud_usage_reserve')) == 1
    assert not ledger.artifacts('cloud_usage_result')
    before = _financial_rows(ledger)
    def no_resend(*args, **kwargs):
        pytest.fail('unknown paid input must not be sent again')
    monkeypatch.setattr(AnthropicClient, 'request', no_resend)
    again = _cloud_pass(ledger)
    assert again['done'] == 0 and _financial_rows(ledger) == before
    assert len(local_calls) == 1 and not cloud_calls
    current, _meta = _current(ledger)
    assert current['artifact_id'] == local['artifact_id'] and current['content'] == local['content']


def test_source_edit_between_selection_and_claim_cannot_send_old_input(local_then_cloud, monkeypatch):
    ledger, cfg, _local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    original = worker._claim
    changed = []
    def edit_before_claim(db, row, *args, **kwargs):
        if not changed:
            db.save_messages([_message(body='完全合成の訂正後の経過記録です。')])
            changed.append(row['message_id'])
        return original(db, row, *args, **kwargs)
    monkeypatch.setattr(worker, '_claim', edit_before_claim)
    assert _cloud_pass(ledger)['done'] == 0
    assert changed == [1] and not cloud_calls and not _financial_rows(ledger)


def _selected_contract(cfg):
    selection, errors = cloud_policy.resolve_profile(cfg, route='reextract', purpose='backlog')
    assert not errors
    return worker.cloud_inference.cloud_provider_contract(selection=selection)


@pytest.mark.parametrize('change', ['contract', 'content', 'missing_provenance', 'model', 'source'])
def test_cloud_completion_needs_exact_contract_source_and_provider_seal(local_then_cloud, change):
    ledger, cfg, _local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    assert _cloud_pass(ledger)['done'] == 1
    cloud, meta = _current(ledger)
    source = dict(ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    contract = _selected_contract(cfg)
    assert worker.cloud_completion(ledger.db, source, contract) == 'complete'
    with ledger.db:
        if change == 'contract':
            contract = {**contract, 'prompt': contract['prompt'] + '-different-profile'}
        elif change == 'content':
            document = json.loads(cloud['content'])
            document['summary'] = '完全合成の改変候補'
            ledger.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?',
                              (json.dumps(document), cloud['artifact_id']))
        elif change == 'missing_provenance':
            meta.pop('provider_provenance')
            ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?',
                              (json.dumps(meta), cloud['artifact_id']))
        elif change == 'model':
            ledger.db.execute("UPDATE artifacts SET model='synthetic-wrong-model' WHERE artifact_id=?",
                              (cloud['artifact_id'],))
        else:
            ledger.save_messages([_message(body='完全合成の異なる現在入力です。')])
            source = dict(ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    before = _financial_rows(ledger)
    assert worker.cloud_completion(ledger.db, source, contract) is None
    assert len(cloud_calls) == 1 and _financial_rows(ledger) == before


def test_quality_attention_is_not_cloud_complete_and_is_not_rebilled(local_then_cloud):
    ledger, cfg, _local_calls, cloud_calls, answers = local_then_cloud
    body = '本人は合成薬Aを中止したかもしれません。'
    ledger.save_messages([_message(body=body)])
    with ledger.db:
        ledger.db.execute('UPDATE messages SET first_seen=?', (time.time() - 1200,))
        ledger.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 1200,))
        cloud_policy.capture_history_tx(ledger.db, 'synthetic-quality-attention', time.time())
    answers['local'] = answers['cloud'] = {'symptoms': [], 'meds': [{'name': '合成薬A', 'dose': None,
        'status': 'current', 'subject': 'patient', 'action': 'stop', 'negated': False, 'evidence': body}]}
    assert _local_pass(ledger)['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    assert _cloud_pass(ledger)['done'] == 1
    source = ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()
    contract = _selected_contract(cfg)
    assert worker.cloud_completion(ledger.db, source, contract) == 'attention'
    assert worker.progress_for_message(ledger.db, {**dict(source), '_provider_contract': contract},
                                      model=contract['model'])['state'] != 'complete'
    before, count = _financial_rows(ledger), len(cloud_calls)
    assert _cloud_pass(ledger)['done'] == 0
    assert _financial_rows(ledger) == before and len(cloud_calls) == count


def test_completed_cloud_prefix_does_not_consume_followup_selection_limit(local_then_cloud):
    ledger, cfg, _local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    assert _cloud_pass(ledger)['done'] == 1
    first_cloud = len(cloud_calls)
    cfg['cloud_llm']['mode'] = 'off'
    ledger.save_messages([_message(2, body='完全合成の後続記録です。', posted_at='2026-09-18T00:00:00+09:00')])
    assert _local_pass(ledger)['done'] == 1
    with ledger.db:
        ledger.db.execute('UPDATE messages SET first_seen=?', (time.time() - 1200,))
        ledger.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 1200,))
        cloud_policy.capture_history_tx(ledger.db, 'synthetic-followup-tail', time.time())
    cfg['cloud_llm']['mode'] = 'on'
    assert _cloud_pass(ledger)['done'] == 1
    assert len(cloud_calls) == first_cloud + 1
    rows = _current_artifacts(ledger)
    assert len(rows) == 2
    assert all(json.loads(row['meta'])['provider_provenance']['providers'] == ['anthropic'] for row in rows)



def test_hold_created_after_selection_prevents_cloud_claim(local_then_cloud, monkeypatch):
    ledger, cfg, _local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    local, _meta = _current(ledger)
    cfg['cloud_llm']['mode'] = 'on'
    original = worker._claim
    reached = []
    before = _financial_rows(ledger)
    def concurrent_hold(db, row, *args, **kwargs):
        if not reached:
            with db.db:
                worker._record_hold_tx(db, row, local, 'protected_loss')
            reached.append(row['message_id'])
        return original(db, row, *args, **kwargs)
    monkeypatch.setattr(worker, '_claim', concurrent_hold)
    result = _cloud_pass(ledger)
    assert reached == [1] and result['done'] == 0
    assert not cloud_calls and _financial_rows(ledger) == before
    assert not ledger.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='extract_claim'").fetchone()
    current, _meta = _current(ledger)
    assert current['artifact_id'] == local['artifact_id']
    assert len(ledger.artifacts(worker.UPGRADE_KIND)) == 1
