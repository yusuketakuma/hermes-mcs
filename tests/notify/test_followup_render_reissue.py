"""Real local/cloud proofs reissue ordinary cards without bypassing unsettled sends."""
from copy import deepcopy
import json
import time

import pytest

import notify_cards
from test_cloud_after_local import local_then_cloud, routed, _local_pass, _cloud_pass
from notify_testkit import CFG, NOW, _begin, _card, _dispatch, _intent, _latest_render, _receipt, _settle_bodies

__all__ = ['local_then_cloud', 'routed']


def _header(render):
    spec = json.loads(render['spec_json'])
    return next(item for item in spec['parts']['containers'] if item.get('provider_scope') == 'header')


def _pending_card(local_then_cloud):
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
    assert header['provider_states']['anthropic'] == 'incomplete'
    assert header['processing_status'] != 'complete'
    return ledger, cfg, calls, render


def test_cloud_commit_reissues_delivered_pending_progress_through_normal_render(local_then_cloud, monkeypatch):
    ledger, cfg, calls, before = _pending_card(local_then_cloud)
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
    assert _cloud_pass(ledger)['done'] == 1
    notify_cards.sweep(ledger, cfg, now=time.time())
    after = _latest_render(ledger)
    assert reached, 'normal extraction refresh or sweep must issue the changed presentation'
    assert after['render_rev'] > before['render_rev'] and after['delivery_id'] != before['delivery_id']
    assert after['op'] == 'update' and after['state'] == 'queued'
    assert _header(after)['provider_states']['anthropic'] == 'done'
    assert _header(after)['provider_states']['local'] != 'done'
    assert _header(before)['provider_states']['anthropic'] == 'incomplete'
    card = _card(ledger)
    assert (card['source_generation'], card['presentation_generation']) != (
        prior_card['source_generation'], prior_card['presentation_generation'])
    count = ledger.db.execute('SELECT COUNT(*) FROM notification_renders').fetchone()[0]
    notify_cards.sweep(ledger, cfg, now=time.time() + 1)
    assert ledger.db.execute('SELECT COUNT(*) FROM notification_renders').fetchone()[0] == count
    assert len(calls) == 1


@pytest.mark.parametrize('state', ['inflight', 'unknown'])
def test_cloud_progress_change_never_reposts_an_unsettled_card(local_then_cloud, state):
    ledger, cfg, calls, before = _pending_card(local_then_cloud)
    _begin(ledger, before, n=4001, cfg=cfg)
    if state == 'unknown':
        _receipt(ledger, before, f'{4001:016x}', result='unknown', message_id=None,
                 error_code='transport_timeout', n=4002, cfg=cfg)
    prior = dict(_latest_render(ledger))
    assert prior['state'] == ('unknown' if state == 'unknown' else 'sending')
    attempts = [tuple(row) for row in ledger.db.execute(
        'SELECT * FROM notification_delivery_attempts ORDER BY attempt_id')]
    assert _cloud_pass(ledger)['done'] == 1
    notify_cards.sweep(ledger, cfg, now=NOW + 1)
    after = _latest_render(ledger)
    assert after['delivery_id'] == before['delivery_id'] and after['render_rev'] == before['render_rev']
    assert after['state'] == prior['state'] and after['spec_json'] == before['spec_json']
    assert ledger.db.execute('SELECT COUNT(*) FROM notification_renders').fetchone()[0] == 1
    assert [tuple(row) for row in ledger.db.execute(
        'SELECT * FROM notification_delivery_attempts ORDER BY attempt_id')] == attempts
    assert len(calls) == 1
