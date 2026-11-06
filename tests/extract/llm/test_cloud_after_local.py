"""Synthetic local completion is final: the individual cloud lane stays closed."""
import extract_llm_context
import extract_llm_prepare
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_store
from contextlib import nullcontext
import json
import time

import pytest

import cloud_inference
import cloud_policy
import extract_llm as worker
import mcs_util
import semantic_candidate
import semantic_policy
from candidate_testkit import approved_config
from extract_testkit import _message
from test_anthropic_runtime import AnthropicClient, _current_artifacts, _use_real_cloud_accounting, routed
from test_cloud_response_recovery import legacy_cloud_routes

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


def _cloud_pass(ledger):
    return worker.run_pending(ledger, limit=1, workers=1, budget_s=180,
                              cloud_only=True, background=True)


def _current(ledger):
    rows = _current_artifacts(ledger)
    assert len(rows) == 1
    return rows[0], json.loads(rows[0]['meta'])


def test_local_completion_is_final_and_cloud_lane_stays_closed(local_then_cloud):
    ledger, cfg, local_calls, cloud_calls, _answers = local_then_cloud
    first = _local_pass(ledger)
    assert first['done'] == 1 and local_calls and not cloud_calls
    local, meta = _current(ledger)
    assert meta['provider_provenance']['providers'] == ['local']
    cfg['cloud_llm']['mode'] = 'on'
    second = _cloud_pass(ledger)
    assert second['done'] == 0, 'individual cloud extraction is closed; the local result stands'
    assert not cloud_calls
    assert not ledger.artifacts('cloud_usage_reserve') and not ledger.artifacts('cloud_usage_result')
    current, meta = _current(ledger)
    assert current['artifact_id'] == local['artifact_id']
    assert meta['provider_provenance']['providers'] == ['local']
    assert _cloud_pass(ledger)['done'] == 0 and not cloud_calls


def _financial_rows(ledger):
    return [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result',"
        "'cloud_usage_disposition') ORDER BY artifact_id")]


def test_closed_cloud_lane_never_reaches_claim_or_publish(local_then_cloud, monkeypatch):
    ledger, cfg, local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    local, _meta = _current(ledger)
    cfg['cloud_llm']['mode'] = 'on'
    reached = []
    for target in ('_claim', '_commit_extraction_atomic'):
        original = getattr(extract_llm_runner, target)
        def spy(db, row, *args, _original=original, _name=target, **kwargs):
            reached.append(_name)
            return _original(db, row, *args, **kwargs)
        monkeypatch.setattr(extract_llm_runner, target, spy)
    report = _cloud_pass(ledger)
    assert report['done'] == 0 and not reached
    assert not cloud_calls and not _financial_rows(ledger)
    current, _meta = _current(ledger)
    assert current['artifact_id'] == local['artifact_id']
    assert len(local_calls) == 1


def test_protected_local_prior_is_final_without_cloud_completion_or_resend(local_then_cloud):
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
    assert report['done'] == 0 and not cloud_calls, (
        'the closed lane cannot overwrite the protected local prior')
    assert not _financial_rows(ledger)
    current, meta = _current(ledger)
    assert current['artifact_id'] == local['artifact_id'] and current['content'] == local['content']
    assert meta['provider_provenance']['providers'] == ['local']
    assert _cloud_pass(ledger)['done'] == 0 and not cloud_calls


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
    with legacy_cloud_routes(), pytest.raises(KeyboardInterrupt):
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


def test_paid_cloud_answer_is_preserved_without_resend_under_closed_route(local_then_cloud):
    ledger, cfg, _local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    with legacy_cloud_routes():
        assert _cloud_pass(ledger)['done'] == 1
    assert len(cloud_calls) == 1
    assert len(ledger.artifacts('cloud_usage_reserve')) == 1
    assert len(ledger.artifacts('cloud_usage_result')) == 1
    financial = _financial_rows(ledger)
    current, meta = _current(ledger)
    assert meta['provider_provenance']['providers'] == ['anthropic']
    assert _cloud_pass(ledger)['done'] == 0 and len(cloud_calls) == 1
    assert _financial_rows(ledger) == financial
    assert _current(ledger)[0]['artifact_id'] == current['artifact_id']


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
    monkeypatch.setattr(extract_llm_runner, '_claim', worker._claim)
    assert _cloud_pass(ledger)['done'] == 0
    assert changed == [] and not cloud_calls and not _financial_rows(ledger)


def _selected_contract(cfg):
    selection, errors = cloud_policy.resolve_profile(cfg, route='reextract', purpose='backlog')
    assert not errors
    return worker.cloud_inference.cloud_provider_contract(selection=selection)


@pytest.mark.parametrize('change', ['contract', 'content', 'missing_provenance', 'model', 'source'])
def test_cloud_completion_needs_exact_contract_source_and_provider_seal(local_then_cloud, change):
    ledger, cfg, _local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    with legacy_cloud_routes():
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
    with legacy_cloud_routes():
        assert _cloud_pass(ledger)['done'] == 1
    source = ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()
    contract = _selected_contract(cfg)
    assert worker.cloud_completion(ledger.db, source, contract) == 'attention'
    assert worker.progress_for_message(ledger.db, {**dict(source), '_provider_contract': contract},
                                      model=contract['model'])['state'] != 'complete'
    before, count = _financial_rows(ledger), len(cloud_calls)
    assert _cloud_pass(ledger)['done'] == 0
    assert _financial_rows(ledger) == before and len(cloud_calls) == count


def test_local_followup_completes_without_any_cloud_selection(local_then_cloud):
    ledger, cfg, _local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    cfg['cloud_llm']['mode'] = 'off'
    ledger.save_messages([_message(2, body='完全合成の後続記録です。', posted_at='2026-09-18T00:00:00+09:00')])
    with ledger.db:
        ledger.db.execute('UPDATE messages SET first_seen=? WHERE message_id=2', (time.time() - 1200,))
        ledger.db.execute('UPDATE message_revisions SET observed_at=? WHERE message_id=2', (time.time() - 1200,))
        cloud_policy.capture_history_tx(ledger.db, 'synthetic-followup-tail', time.time())
    assert _local_pass(ledger)['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    assert _cloud_pass(ledger)['done'] == 0 and not cloud_calls
    rows = _current_artifacts(ledger)
    assert len(rows) == 2
    assert all(json.loads(row['meta'])['provider_provenance']['providers'] == ['local'] for row in rows)



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


def test_canonical_projection_leaves_closed_cloud_lane_idle(local_then_cloud):
    ledger, cfg, local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    local, _meta = _current(ledger)
    v4_id = _seal_canonical_v4(ledger)
    sealed_v4 = _v4_row(ledger, v4_id)
    cfg['cloud_llm']['mode'] = 'on'
    report = _cloud_pass(ledger)
    assert report['done'] == 0 and not cloud_calls and not _financial_rows(ledger)
    current, meta = _current(ledger)
    assert current['artifact_id'] == local['artifact_id']
    assert meta['provider_provenance']['providers'] == ['local']
    assert _v4_row(ledger, v4_id) == sealed_v4


@pytest.mark.parametrize('boundary', ['claim', 'publish'])
def test_projection_added_mid_pass_cannot_open_closed_cloud_lane(local_then_cloud, monkeypatch, boundary):
    ledger, cfg, local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    sealed = []
    target = '_claim' if boundary == 'claim' else '_commit_extraction_atomic'
    original = getattr(extract_llm_runner, target)
    def concurrent_seal(db, row, *args, **kwargs):
        if not sealed:
            with db.db:
                sealed.append(_seal_canonical_v4(db))
        return original(db, row, *args, **kwargs)
    monkeypatch.setattr(extract_llm_runner, target, concurrent_seal)
    report = _cloud_pass(ledger)
    assert sealed == [] and report['done'] == 0
    assert not cloud_calls and not _financial_rows(ledger)
    current, meta = _current(ledger)
    assert meta['provider_provenance']['providers'] == ['local']


def _worker_cloud_pass(ledger, cfg, carry=None):
    """The resident backlog worker's exact cloud call (mcs/llm/cloud_backlog.py)."""
    return worker.run_pending(ledger, limit=1, budget_s=900, per_write_lock=True,
                              workers=1, background=True, cloud_only=True,
                              prioritize_patients=True,
                              admitted_ids=worker.cloud_backlog_admissions(ledger, cfg),
                              carry=carry or [])


def test_canonical_sources_complete_locally_while_worker_cloud_lane_stays_closed(
        local_then_cloud, tmp_path, monkeypatch):
    ledger, cfg, _local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    v4_id = _seal_canonical_v4(ledger)
    sealed_v4 = _v4_row(ledger, v4_id)
    ledger.save_messages([_message(mid=2, body='完全合成の経過補記です。',
                                   posted_at='2026-09-19T01:00:00+09:00'),
                          _message(mid=3, body='完全合成の経過補記その2です。',
                                   posted_at='2026-09-19T02:00:00+09:00')])
    with ledger.db:
        ledger.db.execute('UPDATE messages SET first_seen=? WHERE message_id IN (2,3)',
                          (time.time() - 1200,))
        ledger.db.execute('UPDATE message_revisions SET observed_at=? WHERE message_id IN (2,3)',
                          (time.time() - 1200,))
        cloud_policy.capture_history_tx(ledger.db, 'synthetic-canonical-raw', time.time())
        v3_id = _seal_canonical_v4(ledger, 3)   # seal commits; keep it last in the block
    monkeypatch.setattr(mcs_util, 'HOME', str(tmp_path))
    monkeypatch.setattr(semantic_candidate, '_LOADED_SOURCES', None)
    cfg = {**cfg, 'semantic': {'mode': 'shadow',
        'fact_source': 'canonical', 'project_ids': [1]}}
    cfg['cloud_llm']['mode'] = 'on'   # candidate_fingerprint covers cloud_llm
    cfg = approved_config(cfg)
    assert not semantic_policy.semantic_config(cfg, key_present=True)[1]
    monkeypatch.setattr(worker, 'load_config', lambda: cfg)
    monkeypatch.setattr(extract_llm_context, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', worker.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: cfg)
    # The generic legacy admission set is closed under canonical — that
    # closure used to leave the required cloud pass with zero targets. The
    # closed individual cloud lane now selects nothing at all, while local
    # extraction still completes every pending source.
    assert worker.legacy_admissions(ledger, cfg) == set()
    assert worker.cloud_backlog_admissions(ledger, cfg) is None
    report = worker.run_pending(ledger, limit=3, workers=1, budget_s=180,
                                allow_cloud=False)
    assert report['done'] == 1   # only the unsealed raw msg2 owes local extraction
    for _ in range(2):
        assert _worker_cloud_pass(ledger, cfg)['done'] == 0
    assert not cloud_calls and not _financial_rows(ledger)
    done_ids = sorted(row[0] for row in ledger.db.execute(
        "SELECT DISTINCT message_id FROM artifacts WHERE kind='extract_llm'"))
    assert done_ids == [1, 2]   # msg3 stays canonical-owned via its v4 seal
    metas = {row['message_id']: json.loads(row['meta']) for row in _current_artifacts(ledger)}
    assert all(meta['provider_provenance']['providers'] == ['local']
               for meta in metas.values())
    assert _v4_row(ledger, v4_id) == sealed_v4 and _v4_row(ledger, v3_id) is not None


@pytest.mark.parametrize('semantic', ["typo", {"fact_source": "canonical"}, {"fact_source": "legacy"}])
def test_semantic_gate_failures_never_smuggle_sealed_source_into_worker(local_then_cloud,
                                                                       semantic, monkeypatch):
    ledger, cfg, _local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    _seal_canonical_v4(ledger)
    cfg = {**cfg, 'semantic': semantic}
    cfg['cloud_llm']['mode'] = 'on'
    monkeypatch.setattr(worker, 'load_config', lambda: cfg)
    monkeypatch.setattr(extract_llm_context, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', worker.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: cfg)
    admitted = worker.cloud_backlog_admissions(ledger, cfg)
    if semantic == {"fact_source": "legacy"}:
        assert admitted is None   # noncanonical keeps the unrestricted lane
        return
    assert admitted == set()      # typo and gate-less canonical stay fail-closed
    report = _worker_cloud_pass(ledger, cfg)
    assert report['done'] == 0 and not cloud_calls


def test_hold_still_vetoes_cloud_pass_on_a_sealed_source(local_then_cloud):
    ledger, cfg, _local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    local, _meta = _current(ledger)
    with ledger.db:
        worker._record_hold_tx(ledger, dict(ledger.db.execute(
            'SELECT * FROM messages WHERE message_id=1').fetchone()), local, 'protected_loss')
    v4_id = _seal_canonical_v4(ledger)
    cfg['cloud_llm']['mode'] = 'on'
    before = _financial_rows(ledger)
    report = _cloud_pass(ledger)
    assert report['done'] == 0 and not cloud_calls and _financial_rows(ledger) == before
    assert _v4_row(ledger, v4_id) is not None
    current, meta = _current(ledger)
    assert current['artifact_id'] == local['artifact_id']
    assert meta['provider_provenance']['providers'] == ['local']


def test_low_signal_greeting_is_not_settled_by_the_closed_cloud_lane(local_then_cloud, monkeypatch):
    ledger, cfg, _local_calls, cloud_calls, _answers = local_then_cloud
    monkeypatch.setattr(worker, '_prefilter_enabled', lambda: True)
    monkeypatch.setattr(extract_llm_runner, '_prefilter_enabled', worker._prefilter_enabled)
    ledger.save_messages([_message(2, body='こんにちは。', posted_at='2026-09-19T00:00:00+09:00')])
    with ledger.db:
        ledger.db.execute('UPDATE messages SET first_seen=?', (time.time() - 1200,))
        ledger.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 1200,))
        cloud_policy.capture_history_tx(ledger.db, 'synthetic-low-signal-followup', time.time())
    cfg['cloud_llm']['mode'] = 'on'
    report = worker.run_pending(ledger, limit=2, workers=1, budget_s=180,
                                cloud_only=True, background=True)
    assert report['done'] == 0 and not cloud_calls and not _financial_rows(ledger)
    worker.run_pending(ledger, limit=2, workers=1, budget_s=180, allow_cloud=False)
    rows = ledger.db.execute(
        "SELECT meta FROM artifacts WHERE kind=? AND message_id=2 ORDER BY artifact_id DESC",
        (worker.KIND,)).fetchall()
    assert rows
    meta = json.loads(rows[0]['meta'])
    assert meta.get('provider_provenance', {}).get('providers') != ['anthropic']


def test_hold_does_not_block_the_closed_cloud_lane(local_then_cloud):
    ledger, cfg, _local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    local, _meta = _current(ledger)
    with ledger.db:
        worker._record_hold_tx(ledger, dict(ledger.db.execute(
            'SELECT * FROM messages WHERE message_id=1').fetchone()), local, 'protected_loss')
    cfg['cloud_llm']['mode'] = 'on'
    before = _financial_rows(ledger)
    result = _cloud_pass(ledger)
    assert result['done'] == 0 and not cloud_calls and _financial_rows(ledger) == before
    current, _meta = _current(ledger)
    assert current['artifact_id'] == local['artifact_id']
    assert len(ledger.artifacts(worker.UPGRADE_KIND)) == 1
