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


@pytest.mark.parametrize('route,expected', [('local', '💻'), ('cloud', '☁️')])
@pytest.mark.parametrize('layout', [1, 2, 4])
def test_thread_heading_uses_only_visible_result(led, route, expected, layout):
    _seed_thread(led, mids=(100,))
    _artifact(led, 100, route)
    _dispatch(led, _intent(led, payload={'message_ids': [100]}))
    content, heading = _heading(led, _card(led), layout)
    assert heading.startswith(expected + ' ')
    assert content['shown'] == [100]
    assert 'synthetic-local' not in heading and 'anthropic' not in heading


def test_cloud_to_local_changes_heading_and_fingerprint(led):
    _seed_thread(led, mids=(100,))
    _artifact(led, 100, 'cloud')
    _dispatch(led, _intent(led, payload={'message_ids': [100]}))
    first, heading = _heading(led, _card(led))
    assert heading.startswith('☁️ ')
    _artifact(led, 100, 'local')
    second, heading = _heading(led, _card(led))
    assert heading.startswith('💻 ')
    assert notify_render.content_fingerprint(first) != notify_render.content_fingerprint(second)


def test_missing_stale_wrong_project_and_legacy_never_claim_route(led):
    _seed_thread(led, mids=(100,))
    _artifact(led, 100, 'cloud', hash_='0' * 64)
    _seed_thread(led, pid=2, root=200, mids=(200,))
    _artifact(led, 200, 'cloud', project=2)
    _artifact(led, 100, 'cloud', version='0.1')
    assert processing_route.icon(led.db, 1, [100]) == '💬'
    assert processing_route.icon(led.db, 1, [999]) == '💬'
    assert processing_route.icon(led.db, 1, [200]) == '💬'


def test_mixed_and_unknown_posts_are_not_mislabelled(led):
    _seed_thread(led)
    _artifact(led, 100, 'local')
    _artifact(led, 101, 'cloud')
    assert processing_route.icon(led.db, 1, [100, 101]) == '💬'
    assert processing_route.icon(led.db, 1, [100, 999]) == '💬'


def test_shadow_comparison_and_malformed_contract_not_cloud(led):
    _seed_thread(led, mids=(100,))
    _artifact(led, 100, 'local')
    led.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                   "VALUES('cloud_compare_v1',1,100,'{}',?,?,?)",
                   (cloud_policy.MODEL, '{}', NOW))
    led.db.commit()
    assert processing_route.icon(led.db, 1, [100]) == '💻'
    _artifact(led, 100, 'cloud')
    led.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.prompt','unverified') "
                   "WHERE artifact_id=(SELECT MAX(artifact_id) FROM artifacts)")
    led.db.commit()
    assert processing_route.icon(led.db, 1, [100]) == '💬'


def test_card_has_no_processing_claim_without_result(led):
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={'message_ids': [100]}))
    _, heading = _heading(led, _card(led))
    assert heading.startswith('💬 ')


def test_layout4_root_and_reply_use_only_their_own_route(led):
    _seed_thread(led)
    _artifact(led, 100, 'local')
    _artifact(led, 101, 'cloud')
    _dispatch(led, _intent(led, payload={'message_ids': [100, 101]}))
    card = _card(led)
    root, root_heading = _heading(led, {**card, 'anchor_key': '{}'}, 4)
    reply, reply_heading = _heading(led, {**card, 'anchor_key': json.dumps({'source': 101})}, 4)
    assert root['shown'] == [100] and root_heading.startswith('💻 ')
    assert reply['shown'] == [101] and reply_heading.startswith('☁️ ')


def test_current_projection_shadows_older_extract_without_route_claim(led):
    from semantic_projection import PROJECTION_VERSION
    _seed_thread(led, mids=(100,))
    _artifact(led, 100, 'cloud')
    meta = {VERSION_KEY: CURRENT_VERSION, 'hash': f'{100:064x}', 'projection_version': PROJECTION_VERSION}
    led.db.execute('INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) VALUES(?,?,?,?,?,?,?)',
                   ('canonical_projection', 1, 100, '{"summary":"synthetic projected"}',
                    'synthetic-local', json.dumps(meta), NOW))
    led.db.commit()
    assert processing_route.icon(led.db, 1, [100]) == '💬'


@pytest.mark.parametrize('provider_name', ['anthropic', 'openai'])
def test_local_to_cloud_updates_one_icon_without_provider_label(led, provider_name):
    _seed_thread(led, mids=(100,))
    _artifact(led, 100, 'local')
    _dispatch(led, _intent(led, payload={'message_ids': [100]}))
    before, heading = _heading(led, _card(led))
    assert heading.startswith('💻 ')
    _artifact(led, 100, 'cloud', provider_name=provider_name)
    after, heading = _heading(led, _card(led))
    assert heading.startswith('☁️ ') and '💻' not in heading
    assert provider_name not in heading
    assert notify_render.content_fingerprint(before) != notify_render.content_fingerprint(after)


def test_openai_reply_switches_to_local_and_refreshes_fingerprint(led):
    _seed_thread(led)
    _artifact(led, 101, 'cloud', provider_name='openai')
    _dispatch(led, _intent(led, payload={'message_ids': [100, 101]}))
    reply = {**_card(led), 'anchor_key': json.dumps({'source': 101})}
    before, heading = _heading(led, reply)
    assert before['shown'] == [101] and heading.startswith('☁️ ')
    _artifact(led, 101, 'local')
    after, heading = _heading(led, reply)
    assert heading.startswith('💻 ')
    assert notify_render.content_fingerprint(before) != notify_render.content_fingerprint(after)
