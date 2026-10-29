"""Only source-bound visible results choose the chat heading icon."""
import json

import pytest

import cloud_policy
import extract_llm
from inference_version import CURRENT_VERSION, VERSION_KEY
import notify_render
import processing_route
from notify_testkit import CFG, NOW, _card, _dispatch, _intent, _seed_thread, led as led


def _artifact(led, mid, route, *, hash_=None, project=1, version=CURRENT_VERSION, content=None, provider_name="anthropic"):
    if route == 'cloud':
        model = cloud_policy.PROVIDERS[provider_name]
        provider = extract_llm._cloud_contract({'cloud_llm': {'mode': 'off', 'provider': provider_name}})
    else:
        model, provider = 'synthetic-local', {}
    meta = {VERSION_KEY: version, 'hash': hash_ or f'{mid:064x}', 'actual_model': model, **provider}
    led.db.execute('INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) VALUES(?,?,?,?,?,?,?)',
                   ('extract_llm', project, mid, json.dumps(content or {'summary': 'synthetic summary'}),
                    model, json.dumps(meta), NOW))
    led.db.commit()


def _heading(led, card, layout=4):
    content = notify_render.card_content(led.db, {**card, 'layout': layout}, cfg=CFG)
    return content, content['containers'][0]['text']


@pytest.mark.parametrize('route', ['local', 'cloud'])
@pytest.mark.parametrize('layout', [1, 2, 4])
def test_thread_heading_keeps_fixed_icon_and_states_are_per_post(led, route, layout):
    _seed_thread(led, mids=(100,))
    _artifact(led, 100, route)
    _dispatch(led, _intent(led, payload={'message_ids': [100]}))
    content, heading = _heading(led, _card(led), layout)
    assert heading.startswith('💬 ')
    assert content['shown'] == [100]
    assert 'synthetic-local' not in heading and 'anthropic' not in heading
    [post] = [c for c in content['containers'] if c.get('rule')]
    assert post['provider_states'] == dict.fromkeys(processing_route.PROVIDERS, 'unknown')


def test_legacy_route_does_not_claim_provider_without_result_evidence(led):
    _seed_thread(led, mids=(100,))
    _artifact(led, 100, 'cloud')
    _dispatch(led, _intent(led, payload={'message_ids': [100]}))
    first, heading = _heading(led, _card(led))
    assert heading.startswith('💬 ')
    _artifact(led, 100, 'local')
    second, heading = _heading(led, _card(led))
    assert heading.startswith('💬 ')
    assert [c['provider_states'] for c in first['containers'] if c.get('rule')] == [
        c['provider_states'] for c in second['containers'] if c.get('rule')]


def test_missing_stale_wrong_project_and_legacy_never_claim_route(led):
    _seed_thread(led, mids=(100,))
    _artifact(led, 100, 'cloud', hash_='0' * 64)
    _seed_thread(led, pid=2, root=200, mids=(200,))
    _artifact(led, 200, 'cloud', project=2)
    _artifact(led, 100, 'cloud', version='0.1')
    for mid in (100, 999, 200):
        assert set(processing_route.provider_states(led.db, 1, mid).values()) == {'unknown'}


def test_mixed_and_unknown_posts_are_not_mislabelled(led):
    _seed_thread(led)
    _artifact(led, 100, 'local')
    _artifact(led, 101, 'cloud')
    for mid in (100, 101, 999):
        assert set(processing_route.provider_states(led.db, 1, mid).values()) == {'unknown'}


def test_shadow_comparison_and_malformed_contract_not_cloud(led):
    _seed_thread(led, mids=(100,))
    _artifact(led, 100, 'local')
    led.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                   "VALUES('cloud_compare_v1',1,100,'{}',?,?,?)",
                   (cloud_policy.MODEL, '{}', NOW))
    led.db.commit()
    assert set(processing_route.provider_states(led.db, 1, 100).values()) == {'unknown'}
    _artifact(led, 100, 'cloud')
    led.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.prompt','unverified') "
                   "WHERE artifact_id=(SELECT MAX(artifact_id) FROM artifacts)")
    led.db.commit()
    assert set(processing_route.provider_states(led.db, 1, 100).values()) == {'unknown'}


def test_card_has_no_processing_claim_without_result(led):
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={'message_ids': [100]}))
    _, heading = _heading(led, _card(led))
    assert heading.startswith('💬 ')


def test_post_states_are_separate_and_update_the_visible_fingerprint(led, monkeypatch):
    _seed_thread(led)
    _dispatch(led, _intent(led, payload={'message_ids': [100, 101]}))
    current = {100: dict.fromkeys(processing_route.PROVIDERS, 'unknown'),
               101: dict.fromkeys(processing_route.PROVIDERS, 'unknown')}
    current[100].update(anthropic='done', jev='partial')
    current[101].update(local='done', jev='skipped')
    monkeypatch.setattr(processing_route, 'provider_states',
                        lambda db, pid, mid, cfg=None, **kwargs: dict(current[mid]))
    card = _card(led)
    before, heading = _heading(led, card, 2)
    posts = [c for c in before['containers'] if c.get('rule')]
    assert heading.startswith('💬 ') and len(posts) == 2
    assert posts[0]['provider_states']['anthropic'] == 'done'
    assert posts[0]['provider_states']['jev'] == 'partial'
    assert posts[1]['provider_states']['local'] == 'done'
    assert posts[1]['provider_states']['jev'] == 'skipped'
    current[101]['jev'] = 'done'
    after, _ = _heading(led, card, 2)
    assert notify_render.content_fingerprint(before) != notify_render.content_fingerprint(after)


def test_deleted_post_has_no_provider_status(led):
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={'message_ids': [100]}))
    led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=100")
    led.db.commit()
    content, heading = _heading(led, _card(led))
    assert heading.startswith('💬 ')
    assert all('provider_states' not in c for c in content['containers'])


@pytest.mark.parametrize('provider', ['local', 'anthropic', 'openai'])
def test_current_extract_requires_complete_result_and_source_bound_provenance(led, provider):
    import inference_dependency_loss
    import mcs_util

    _seed_thread(led, mids=(100,))
    _artifact(led, 100, 'local' if provider == 'local' else 'cloud',
              provider_name='anthropic' if provider == 'local' else provider)
    row = led.db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind='extract_llm' "
                         "AND message_id=100 ORDER BY artifact_id DESC LIMIT 1").fetchone()
    binding = inference_dependency_loss.source_binding(led.db, 1, 100)
    meta = json.loads(row['meta'])
    meta['source_binding'] = binding
    meta['provider_provenance'] = {'version': 1, 'stage': 'extract', 'providers': [provider],
                                   'complete': True, 'source_binding': binding,
                                   'result_hash': mcs_util.payload_hash(json.loads(row['content']))}
    led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), row['artifact_id']))
    led.db.commit()
    states = processing_route.provider_states(led.db, 1, 100)
    assert states[provider] == 'done' and states['jev'] == 'unknown'
    assert all(states[other] == 'unknown' for other in ('local', 'anthropic', 'openai') if other != provider)
    if provider != 'local':
        damaged = dict(meta, prompt='unverified')
        led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(damaged), row['artifact_id']))
        led.db.commit()
        assert set(processing_route.provider_states(led.db, 1, 100).values()) == {'unknown'}
    meta['provider_provenance']['providers'] = ['anthropic' if provider == 'local' else 'local']
    led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), row['artifact_id']))
    led.db.commit()
    assert set(processing_route.provider_states(led.db, 1, 100).values()) == {'unknown'}
    meta['provider_provenance']['providers'] = [provider]
    meta['provider_provenance']['result_hash'] = '0' * 64
    led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), row['artifact_id']))
    led.db.commit()
    assert set(processing_route.provider_states(led.db, 1, 100).values()) == {'unknown'}
    meta['provider_provenance']['result_hash'] = mcs_util.payload_hash(json.loads(row['content']))
    led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), row['artifact_id']))
    led.db.execute("UPDATE messages SET sender_name='変更された合成名' WHERE message_id=100")
    led.db.commit()
    assert set(processing_route.provider_states(led.db, 1, 100).values()) == {'unknown'}


def test_projection_states_require_current_thread_policy_and_source(led):
    import inference_dependency_loss
    import mcs_util
    import semantic_policy
    import semantic_projection
    import semantic_store
    import semantic_transport
    from notify_testkit import _msg

    _seed_thread(led, mids=(100,))
    cfg = {**CFG, 'semantic': {'mode': 'assist', 'fact_source': 'legacy', 'project_ids': [1]}}
    bundle = semantic_store.thread_bundle(led.db, 1, 100, [100], local_model=semantic_transport.llm_model(cfg))
    scfg, errors = semantic_policy.semantic_config(cfg)
    assert not errors
    policy = semantic_policy.policy_fingerprint(scfg)
    binding = inference_dependency_loss.source_binding(led.db, 1, 100)
    content = {'summary': '完全合成の投影'}
    meta = {VERSION_KEY: CURRENT_VERSION, 'hash': f'{100:064x}',
            'projection_version': semantic_projection.PROJECTION_VERSION,
            'source_binding': binding, 'fingerprint': bundle['source_fingerprint'],
            'policy_fingerprint': policy,
            'provider_provenance': {'version': 1, 'stage': 'projection', 'providers': ['local'],
                'complete': True, 'result_hash': mcs_util.payload_hash(content), 'source_binding': binding,
                'source_fingerprint': bundle['source_fingerprint'], 'policy_fingerprint': policy,
                'target_revision': f'{100:064x}', 'jev': 'partial'}}
    led.db.execute('INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) '
                   'VALUES(?,?,?,?,?,?,?)', ('canonical_projection', 1, 100, json.dumps(content),
                                            'synthetic', json.dumps(meta), NOW))
    led.db.commit()
    # A projection cannot establish its own producer by claiming a route.
    assert set(processing_route.provider_states(led.db, 1, 100, cfg=cfg).values()) == {'unknown'}
    meta['provider_provenance']['providers'] = ['anthropic']
    led.db.execute("UPDATE artifacts SET meta=? WHERE kind='canonical_projection' AND message_id=100",
                   (json.dumps(meta),))
    led.db.commit()
    assert set(processing_route.provider_states(led.db, 1, 100, cfg=cfg).values()) == {'unknown'}
    assert set(processing_route.provider_states(led.db, 1, 100, cfg={**cfg, 'semantic': {'mode': 'assist', 'project_ids': [1],
        'match_threshold': 0.91}}).values()) == {'unknown'}
    _msg(led, 101, parent=100)
    assert set(processing_route.provider_states(led.db, 1, 100, cfg=cfg).values()) == {'unknown'}


def test_extract_jev_state_is_bound_to_current_result_and_distinguishes_partial(led):
    import extract_llm
    import inference_dependency_loss

    _seed_thread(led, mids=(100,))
    _artifact(led, 100, 'local', content={'symptoms': [{'text': '合成症状'}]})
    source = led.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind='extract_llm' "
                            "AND message_id=100 ORDER BY artifact_id DESC LIMIT 1").fetchone()
    meta = json.loads(source['meta'])
    meta.update(source_binding=inference_dependency_loss.source_binding(led.db, 1, 100),
                extract_version=extract_llm.EXTRACT_VERSION)
    led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), source['artifact_id']))
    qc_meta = {VERSION_KEY: CURRENT_VERSION, 'hash': f'{100:064x}',
               'extract_version': extract_llm.EXTRACT_VERSION, 'source_artifact_id': source['artifact_id']}

    def add(kind, content, extra=None):
        led.db.execute('INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) '
                       'VALUES(?,?,?,?,?,?,?)', (kind, 1, 100, json.dumps(content), 'synthetic',
                                                json.dumps({**qc_meta, **(extra or {})}), NOW))
        led.db.commit()

    add('extract_qc_skipped', {'status': 'SKIPPED', 'reason': 'daily_budget'})
    assert processing_route.provider_states(led.db, 1, 100)['jev'] == 'skipped'
    add('extract_qc_skipped', {'status': 'SKIPPED', 'reason': 'daily_budget',
                               'partial_answers': {'s0': {'type': 'noul', 'noul': 0.1}}})
    assert processing_route.provider_states(led.db, 1, 100)['jev'] == 'partial'
    add('extract_qc', {'qc': 'done', 'items': [{'section': 'symptoms', 'index': 0,
                                                'verdict': 'NO_MATCH', 'noul': 0.1}],
                       'coverage': {'checked': 1}}, {'qc': 'done', 'jev_requests': 1})
    assert processing_route.provider_states(led.db, 1, 100)['jev'] == 'done'
    led.db.execute("UPDATE artifacts SET content=json_set(content,'$.items[0].index',99) "
                   "WHERE kind='extract_qc' AND message_id=100")
    led.db.commit()
    assert processing_route.provider_states(led.db, 1, 100)['jev'] == 'unknown'


def test_layout4_root_and_reply_use_only_their_own_route(led):
    _seed_thread(led)
    _artifact(led, 100, 'local')
    _artifact(led, 101, 'cloud')
    _dispatch(led, _intent(led, payload={'message_ids': [100, 101]}))
    card = _card(led)
    root, root_heading = _heading(led, {**card, 'anchor_key': '{}'}, 4)
    reply, reply_heading = _heading(led, {**card, 'anchor_key': json.dumps({'source': 101})}, 4)
    assert root['shown'] == [100] and root_heading.startswith('💬 ')
    assert reply['shown'] == [101] and reply_heading.startswith('💬 ')


def test_current_projection_shadows_older_extract_without_route_claim(led):
    from semantic_projection import PROJECTION_VERSION
    _seed_thread(led, mids=(100,))
    _artifact(led, 100, 'cloud')
    meta = {VERSION_KEY: CURRENT_VERSION, 'hash': f'{100:064x}', 'projection_version': PROJECTION_VERSION}
    led.db.execute('INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) VALUES(?,?,?,?,?,?,?)',
                   ('canonical_projection', 1, 100, '{"summary":"synthetic projected"}',
                    'synthetic-local', json.dumps(meta), NOW))
    led.db.commit()
    assert set(processing_route.provider_states(led.db, 1, 100).values()) == {'unknown'}


@pytest.mark.parametrize('provider_name', ['anthropic', 'openai'])
def test_legacy_model_change_does_not_invent_provider_evidence(led, provider_name):
    _seed_thread(led, mids=(100,))
    _artifact(led, 100, 'local')
    _dispatch(led, _intent(led, payload={'message_ids': [100]}))
    before, heading = _heading(led, _card(led))
    assert heading.startswith('💬 ')
    _artifact(led, 100, 'cloud', provider_name=provider_name)
    after, heading = _heading(led, _card(led))
    assert heading.startswith('💬 ') and '💻' not in heading
    assert provider_name not in heading
    assert [c['provider_states'] for c in before['containers'] if c.get('rule')] == [
        c['provider_states'] for c in after['containers'] if c.get('rule')]


def test_reply_keeps_fixed_heading_without_inventing_provider_evidence(led):
    _seed_thread(led)
    _artifact(led, 101, 'cloud', provider_name='openai')
    _dispatch(led, _intent(led, payload={'message_ids': [100, 101]}))
    reply = {**_card(led), 'anchor_key': json.dumps({'source': 101})}
    before, heading = _heading(led, reply)
    assert before['shown'] == [101] and heading.startswith('💬 ')
    _artifact(led, 101, 'local')
    after, heading = _heading(led, reply)
    assert heading.startswith('💬 ')
    assert [c['provider_states'] for c in before['containers'] if c.get('rule')] == [
        c['provider_states'] for c in after['containers'] if c.get('rule')]
