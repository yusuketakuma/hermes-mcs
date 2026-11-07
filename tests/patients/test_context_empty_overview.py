"""Empty overview checkpoints neither consume generation slots nor hide pending sources."""
import json

import pytest

import anthropic_client
import cloud_inference
import patient_context_plan as plan
import patient_context_progress as progress
import patient_context_quality_receipts as quality
import patient_context_quota as quota
import patient_context_refresh as refresh
import patient_context_refresh_readiness as readiness
import patient_context_refresh_sources as sources
import patient_context_refresh_state as state
from test_context_cloud_update_identity import cloud as cloud, finish, real_cloud as real_cloud, runtime as runtime
from test_patient_context_refresh import document, seed, step


def _generations(led):
    return [tuple(row) for row in led.db.execute(
        'SELECT artifact_id,content,meta FROM artifacts WHERE kind=? ORDER BY artifact_id', (quota.KIND,))]


def _no_cloud_unit(*_args, **_kwargs):
    pytest.fail('an empty overview must not construct a cloud inference unit')


@pytest.mark.parametrize('body_state', ['full', 'deleted'])
def test_whole_empty_cloud_checkpoint_uses_no_model_or_generation(runtime, real_cloud, monkeypatch, body_state):
    led, _, calls = runtime
    cfg, posts = real_cloud
    seed(led, body='', state=body_state, proof=False)
    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', _no_cloud_unit)
    outcome = step(runtime, cfg=cfg, local_admit=lambda: False)
    assert (outcome['status'], outcome['lane']) == ('progressed', 'A')
    assert document(led)['facts'] == document(led)['summary'] == []
    assert readiness.context_view_input(led.db, 1, cfg=cfg) is not None
    coverage = readiness.inference_coverage(led.db, 1, cfg)
    assert coverage['complete'] and coverage['status'] == ('empty' if body_state == 'full' else 'excluded')
    assert not _generations(led) and not calls and not posts
    assert not led.artifacts(state.QUALITY_PASS) and not led.artifacts('cloud_usage_reserve')
    assert step(runtime, cfg=cfg, local_admit=lambda: False)['status'] == 'complete'
    assert progress.read(led.db, 1, cfg)['stage'] == 'inactive'


@pytest.mark.parametrize('body_state', ['full', 'deleted'])
@pytest.mark.parametrize('missing', ['unprocessed', 'acquisition'])
def test_empty_only_partial_does_not_create_an_overview(runtime, real_cloud, monkeypatch, body_state, missing):
    led, _, calls = runtime
    cfg, posts = real_cloud
    seed(led, body='', state=body_state, proof=False)
    if missing == 'unprocessed':
        seed(led, mid=2, body='生活環境: 合成の未解析記録。', proof=False)
    else:
        with led.db:
            led.db.execute("UPDATE patients SET fetch_state='incomplete',history_floor=NULL WHERE project_id=1")
    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', _no_cloud_unit)
    outcome = step(runtime, cfg=cfg, local_admit=lambda: False)
    assert outcome['status'] == 'deferred' and outcome['done'] == 0
    assert document(led) is None and readiness.context_view_input(led.db, 1, cfg=cfg) is None
    assert not readiness.inference_coverage(led.db, 1, cfg)['complete']
    assert progress.read(led.db, 1, cfg)['percent'] != 100
    assert not _generations(led) and not led.artifacts(state.SCAN) and not calls and not posts


def test_useful_source_and_audit_lanes_run_before_partial_overview(runtime, real_cloud):
    led, _, _ = runtime
    cfg, posts = real_cloud
    seed(led, body='', proof=False)
    seed(led, mid=2, body='生活環境: 合成の集合住宅で生活しています。')
    seed(led, mid=3, body='生活環境: 合成の未解析記録。', proof=False)
    lanes = []
    for _ in range(60):
        outcome = step(runtime, cfg=cfg, local_admit=lambda: False)
        lanes.append(outcome.get('lane'))
        view = readiness.context_view_input(led.db, 1, cfg=cfg)
        if view is not None:
            break
        assert (outcome['status'] == 'progressed' or outcome['status'] == 'deferred'
                and outcome.get('reason') in ('source_quality_pending', 'summary_quality_pending')), outcome
        if 'A' not in lanes:
            assert document(led) is None and not _generations(led)
    else:
        pytest.fail('audited useful source never reached partial publication')
    assert lanes[0] == 'B' and lanes.index('C') < lanes.index('A')
    assert view['facts'] and {fact['message_id'] for fact in view['facts']} == {2}
    assert view['coverage']['partial'] and view['coverage']['unreflected'] == 1
    assert len(_generations(led)) == 1
    assert any('source' in payload for payload in posts) and any('reports' in payload for payload in posts)
    assert not readiness.inference_coverage(led.db, 1, cfg)['complete']
    assert progress.read(led.db, 1, cfg)['percent'] != 100


def test_nonempty_source_with_no_facts_still_runs_source_and_summary_quality(runtime, real_cloud, monkeypatch):
    led, _, _ = runtime
    cfg, posts = real_cloud
    seed(led, body='合成の事務連絡です。')
    wire = anthropic_client.AnthropicClient.request

    def empty_answer(client, method, body, timeout, *, deadline):
        status, headers, raw = wire(client, method, body, timeout, deadline=deadline)
        payload = json.loads(body['messages'][0]['content'])
        assert 'source' in payload
        response = json.loads(raw)
        response['content'] = [{'type': 'text', 'text': json.dumps({'patient_context': []})}]
        return status, headers, json.dumps(response).encode()

    monkeypatch.setattr(anthropic_client.AnthropicClient, 'request', empty_answer)
    finish(runtime, cfg)
    snapshot = sources._snapshot(led.db, 1, cfg)
    assert any(plan.source_usable(source) for source in snapshot['sources'])
    assert document(led)['facts'] == document(led)['summary'] == []
    assert posts and all('source' in payload for payload in posts)
    assert led.synthetic_jev_requests and led.artifacts(state.QUALITY_PASS)
    assert len(_generations(led)) == 1 and readiness.inference_coverage(led.db, 1, cfg)['complete']
    # Older empty summaries constructed a cloud unit without sending it. Keep
    # the real audit receipts while reproducing only that stored backend form.
    _, meta = state._latest(led.db, state.KIND, 1)
    audit_key = meta['quality_audit_key']
    meta['contract'] = meta.pop('route_contract')
    meta['provider'] = meta['contract']['provider']
    meta['model'] = meta['provider']['model']
    meta.pop('quality_schedule_key')
    with led.db:
        led.db.execute('UPDATE artifacts SET meta=? WHERE kind=? AND project_id=1',
                       (json.dumps(meta), state.KIND))
    assert readiness.context_view_input(led.db, 1, cfg=cfg) is not None
    count = len(posts)
    outcome = step(runtime, cfg=cfg, local_admit=lambda: False)
    assert (outcome['status'], outcome['lane']) == ('progressed', 'A')
    _, final_meta = state._latest(led.db, state.KIND, 1)
    assert final_meta['quality_audit_key'] == audit_key and 'provider' not in final_meta
    assert readiness.context_view_input(led.db, 1, cfg=cfg) is not None
    assert len(_generations(led)) == 1 and len(posts) == count


def test_empty_source_does_not_bypass_a_prior_quality_failure(runtime, real_cloud):
    led, clock, _ = runtime
    cfg, _ = real_cloud
    seed(led, body='', proof=False)
    identity = {'input_key': 'a' * 64, 'candidate_key': 'b' * 64,
                'failure_key': 'c' * 64, 'audit_key': 'd' * 64, 'fact_ids': ['e' * 64]}
    claim = quality._quality_claim(led, 1, identity['input_key'], clock.mono + 60, lambda *_: True)
    assert claim is not None
    quality._quality_store_answers(led, 1, identity, claim,
        {'e' * 64: {'choice': 'important_missing', 'confidence': 1.0}}, lambda *_: True, 0.9, set())
    quality._quality_release(led, 1, identity['input_key'], claim)
    assert quality._has_prior_quality_failure(led.db, 1)
    snapshot = sources._snapshot(led.db, 1, cfg, source_keys=[1])
    assert refresh._generation_plan(led.db, 1, cfg, snapshot, clock.wall)['needed']
    outcome = step(runtime, cfg=cfg, local_admit=lambda: False)
    assert outcome['status'] == 'progressed' and outcome['lane'] == 'A'
    assert len(_generations(led)) == 1
    assert not readiness.inference_coverage(led.db, 1, cfg)['complete']
    assert readiness.context_view_input(led.db, 1, cfg=cfg) is None


@pytest.mark.parametrize('body_state', ['full', 'deleted'])
def test_empty_replacement_withdraws_old_facts_without_rewriting_generation_history(runtime, real_cloud, monkeypatch, body_state):
    led, _, _ = runtime
    cfg, posts = real_cloud
    finish(runtime, cfg)
    assert document(led)['summary'] and readiness.context_view_input(led.db, 1, cfg=cfg) is not None
    before, sent = _generations(led), len(posts)
    seed(led, body='', state=body_state, proof=False)
    assert readiness.context_view_input(led.db, 1, cfg=cfg) is None
    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', _no_cloud_unit)
    outcome = step(runtime, cfg=cfg, local_admit=lambda: False)
    assert outcome['status'] == 'progressed' and outcome['lane'] == 'A'
    assert document(led)['facts'] == document(led)['summary'] == []
    assert _generations(led) == before and len(posts) == sent
    assert step(runtime, cfg=cfg, local_admit=lambda: False)['status'] == 'complete'
