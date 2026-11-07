"""Closed cloud stays silent; adopted local edits respect unsettled delivery."""
from copy import deepcopy
import json
import time

import pytest

import notify_cards
from test_cloud_after_local import local_then_cloud, routed, _local_pass, _cloud_pass, _current
from notify_testkit import CFG, NOW, _begin, _card, _dispatch, _intent, _latest_render, _receipt, _settle_bodies

__all__ = ['local_then_cloud', 'routed']


def _header(render):
    spec = json.loads(render['spec_json'])
    return next(item for item in spec['parts']['containers'] if item.get('provider_scope') == 'header')


def _local_card(local_then_cloud):
    ledger, cfg, _local_calls, calls, _answers = local_then_cloud
    cfg.update(deepcopy(CFG))
    with ledger.db:
        ledger.db.execute("INSERT INTO patients(project_id,patient_name,is_archived) VALUES(1,'synthetic-patient',0)")
    assert _local_pass(ledger)['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    assert _dispatch(ledger, _intent(ledger, payload={'message_ids': [1]}), cfg)['dispatched']
    render = _latest_render(ledger)
    header = _header(render)
    assert header['provider_states']['local'] == 'done'
    assert header['visible_providers'] == ['jev', 'local']
    assert header['provider_states']['anthropic'] not in ('incomplete', 'attention', 'unverified')
    assert 'Anthropic' not in header['text']
    return ledger, cfg, calls, render


def _closed_cloud_is_silent(ledger):
    financial = [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') ORDER BY artifact_id")]
    renders = [tuple(row) for row in ledger.db.execute('SELECT * FROM notification_renders ORDER BY render_rev')]
    assert _cloud_pass(ledger)['done'] == 0
    assert [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') ORDER BY artifact_id")] == financial
    assert [tuple(row) for row in ledger.db.execute('SELECT * FROM notification_renders ORDER BY render_rev')] == renders


def _adopt_local_edit(local_then_cloud):
    from extract_testkit import _message
    ledger, _cfg, _local_calls, calls, answers = local_then_cloud
    before, _ = _current(ledger)
    body = '本人は合成薬Aを服用していません。'
    ledger.save_messages([_message(body=body)])
    answers['local'] = {'symptoms': [], 'meds': [{'name': '合成薬A', 'dose': None,
        'status': 'current', 'subject': 'patient', 'action': 'none', 'negated': True, 'evidence': body}]}
    assert _local_pass(ledger)['done'] == 1
    current, meta = _current(ledger)
    assert current['artifact_id'] != before['artifact_id']
    assert meta['provider_provenance']['providers'] == ['local']
    medication = json.loads(current['content'])['meds'][0]
    assert medication['name'] == '合成薬A' and medication['negated'] is True
    assert not calls
    return body


def test_local_source_edit_reissues_delivered_card_with_cloud_lane_closed(local_then_cloud, monkeypatch):
    ledger, cfg, calls, before = _local_card(local_then_cloud)
    _begin(ledger, before, n=3001, cfg=cfg)
    _receipt(ledger, before, f'{3001:016x}', n=3002, cfg=cfg)
    _settle_bodies(ledger, before)
    assert _latest_render(ledger)['state'] == 'delivered'
    prior_card = _card(ledger)
    reached = []
    real_issue = notify_cards.issue_render
    def tracked(*args, **kwargs):
        result = real_issue(*args, **kwargs)
        reached.append(result)
        return result
    monkeypatch.setattr(notify_cards, 'issue_render', tracked)
    _closed_cloud_is_silent(ledger)
    assert not calls
    body = _adopt_local_edit(local_then_cloud)
    notify_cards.sweep(ledger, cfg, now=time.time())
    after = _latest_render(ledger)
    assert reached, 'normal extraction refresh or sweep must issue the changed presentation'
    assert after['render_rev'] > before['render_rev'] and after['delivery_id'] != before['delivery_id']
    assert after['op'] == 'update' and after['state'] == 'queued'
    assert _header(after)['provider_states']['local'] == 'done'
    assert _header(after)['visible_providers'] == ['jev', 'local']
    assert _header(after)['context_progress'] == _header(before)['context_progress']
    after_text = json.dumps(json.loads(after['spec_json'])['parts'], ensure_ascii=False)
    before_text = json.dumps(json.loads(before['spec_json'])['parts'], ensure_ascii=False)
    assert body in after_text and body not in before_text
    card = _card(ledger)
    assert (card['source_generation'], card['presentation_generation']) != (
        prior_card['source_generation'], prior_card['presentation_generation'])
    count = ledger.db.execute('SELECT COUNT(*) FROM notification_renders').fetchone()[0]
    notify_cards.sweep(ledger, cfg, now=time.time() + 1)
    assert ledger.db.execute('SELECT COUNT(*) FROM notification_renders').fetchone()[0] == count
    assert not calls


@pytest.mark.parametrize('state', ['inflight', 'unknown'])
def test_local_source_edit_never_reposts_an_unsettled_card(local_then_cloud, state):
    ledger, cfg, calls, before = _local_card(local_then_cloud)
    _begin(ledger, before, n=4001, cfg=cfg)
    if state == 'unknown':
        _receipt(ledger, before, f'{4001:016x}', result='unknown', message_id=None,
                 error_code='transport_timeout', n=4002, cfg=cfg)
    prior = dict(_latest_render(ledger))
    assert prior['state'] == ('unknown' if state == 'unknown' else 'sending')
    attempts = [tuple(row) for row in ledger.db.execute(
        'SELECT * FROM notification_delivery_attempts ORDER BY attempt_id')]
    _closed_cloud_is_silent(ledger)
    assert not calls
    _adopt_local_edit(local_then_cloud)
    notify_cards.sweep(ledger, cfg, now=NOW + 1)
    after = _latest_render(ledger)
    assert after['delivery_id'] == before['delivery_id'] and after['render_rev'] == before['render_rev']
    assert after['state'] == prior['state'] and after['spec_json'] == before['spec_json']
    assert ledger.db.execute('SELECT COUNT(*) FROM notification_renders').fetchone()[0] == 1
    assert [tuple(row) for row in ledger.db.execute(
        'SELECT * FROM notification_delivery_attempts ORDER BY attempt_id')] == attempts
    assert not calls
