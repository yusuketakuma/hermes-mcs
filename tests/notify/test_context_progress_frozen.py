"""Creation-only context observations use synthetic ledgers and no provider calls."""
import copy
import json

import pytest

import notify_card_renders as renders
import notify_cards
import notify_render
import patient_context_progress as progress
import processing_route
from adapters.common import spec as spec_module
from adapters.discord.cards import _zones
from adapters.lineworks import cards as lineworks
from adapters.slack.presentation import render_parts
from notify_testkit import CFG, NOW, _card, _dispatch, _intent, _latest_render, _seed_thread, led
from notify_lineworks_testkit import LINEWORKS
from slack_testkit import SLACK

__all__ = ['led']


def value(percent=73, stage='sources', held=False):
    return progress.observation(percent, stage, held=held, source_sig='1' * 64)


def header(spec):
    return next(c for c in spec['parts']['containers'] if c.get('provider_scope') == 'header')


@pytest.mark.parametrize('cfg', [CFG, SLACK, LINEWORKS])
def test_creation_freezes_shared_compact_display_and_sealed_version(led, monkeypatch, cfg):
    monkeypatch.setattr(progress, 'read', lambda *args: value(73, held=True))
    _seed_thread(led, mids=(100,))
    assert _dispatch(led, _intent(led, payload={'message_ids': [100]}), cfg)['dispatched']
    spec = json.loads(_latest_render(led)['spec_json'])
    assert spec['parts']['processing_display_version'] == 3
    assert header(spec)['context_progress'] == {**value(73, held=True), 'observed_at': NOW}
    assert header(spec)['visible_providers'] == ['jev', 'local']
    assert header(spec)['provider_sequence'] is None
    validator = (__import__('adapters.slack.cards', fromlist=['validate']).validate if cfg is SLACK
                 else lineworks.validate if cfg is LINEWORKS else spec_module.validate)
    validator(spec)
    expected = '👤 73% 保留'
    assert expected in notify_render.display_text(spec['parts'])
    if cfg is SLACK:
        assert expected in str(render_parts(spec['parts'], card_kind='thread'))
    elif cfg is LINEWORKS:
        assert expected in str(lineworks._face_nodes(spec['parts'], []))
    else:
        assert expected in str(_zones(spec, str))
    assert '患者コンテキスト' not in notify_render.display_text(spec['parts'])
    assert '採用結果の処理順' not in notify_render.display_text(spec['parts'])
    assert '☁' not in notify_render.display_text(spec['parts'])
    # Presentation versions are sealed into the card hash, including old-worker refusal.
    old = copy.deepcopy(spec)
    old['parts']['processing_display_version'] = 2
    with pytest.raises(ValueError, match='bad_context_progress'):
        validator(old)


def test_progress_alone_never_changes_fingerprint_or_queues_an_update(led, monkeypatch):
    calls = []
    monkeypatch.setattr(progress, 'read', lambda *args: calls.append(True) or value(20))
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={'message_ids': [100]}))
    before = _latest_render(led)
    fingerprint = notify_render.content_fingerprint(notify_render.card_content(led.db, _card(led), cfg=CFG))
    monkeypatch.setattr(progress, 'read', lambda *args: pytest.fail('frozen progress must not reread'))
    notify_cards.sweep(led, CFG, now=NOW + 1)
    assert _latest_render(led)['delivery_id'] == before['delivery_id']
    assert notify_render.content_fingerprint(notify_render.card_content(led.db, _card(led), cfg=CFG)) == fingerprint
    assert calls == [True]
    # A new sealed plan for a different reason and missing historical spec still reuses the receipt.
    with led.db:
        led.db.execute('UPDATE notification_renders SET spec_json=NULL')
        card = _card(led)
        assert renders._frozen_context_progress(led.db, card, CFG, NOW + 2)['percent'] == 20
    assert len(led.artifacts('patient_context_progress_snapshot_v1')) == 1


def test_snapshot_and_first_spec_roll_back_together(led, monkeypatch):
    monkeypatch.setattr(progress, 'read', lambda *args: value())
    _seed_thread(led, mids=(100,))
    intent = _intent(led, payload={'message_ids': [100]})
    original = renders._persist_render
    def crash(*args, **kwargs):
        raise RuntimeError('synthetic crash before seal')
    monkeypatch.setattr(renders, '_persist_render', crash)
    with pytest.raises(RuntimeError, match='synthetic crash'):
        _dispatch(led, intent)
    assert not led.artifacts('patient_context_progress_snapshot_v1')
    assert led.db.execute('SELECT COUNT(*) FROM notification_renders').fetchone()[0] == 0
    monkeypatch.setattr(renders, '_persist_render', original)
    assert _dispatch(led, intent)['dispatched']
    assert len(led.artifacts('patient_context_progress_snapshot_v1')) == 1


def test_old_card_without_observation_is_unknown_and_scope_mismatch_never_reuses(led, monkeypatch):
    monkeypatch.setattr(progress, 'read', lambda *args: value())
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={'message_ids': [100]}))
    with led.db:
        # Simulate a pre-feature card: no historical observation can be reconstructed.
        led.db.execute("DELETE FROM artifacts WHERE kind='patient_context_progress_snapshot_v1'")
        monkeypatch.setattr(progress, 'read', lambda *args: pytest.fail('no past reconstruction'))
        frozen = renders._frozen_context_progress(led.db, _card(led), CFG, NOW + 1)
        assert frozen['percent'] is None and frozen['observed_at'] == 0
        moved = {**_card(led), 'channel_id': 'synthetic-other-channel'}
        assert renders._frozen_context_progress(led.db, moved, CFG, NOW + 2)['percent'] is None
    assert len(led.artifacts('patient_context_progress_snapshot_v1')) == 1


@pytest.mark.parametrize('change', [
    {'percent': True}, {'percent': 101}, {'percent': -1}, {'percent': 73.0},
    {'stage': 'invalid'}, {'stage': 'complete'}, {'held': 1},
    {'observed_at': True}, {'observed_at': float('nan')}, {'observed_at': float('inf')},
    {'source_sig': 'bad'}, {'extra': 'forbidden'}, {'proof_version': True},
])
def test_progress_contract_rejects_unproven_or_ambiguous_metadata(change):
    assert not processing_route.valid_context_progress({**value(), 'observed_at': NOW, **change})


def test_scope_change_does_not_reuse_a_numeric_observation(led, monkeypatch):
    monkeypatch.setattr(progress, 'read', lambda *args: value())
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={'message_ids': [100]}))
    with led.db:
        led.db.execute('UPDATE notification_meta SET notify_dirty=notify_dirty')
        card = _card(led)
        assert renders._frozen_context_progress(led.db, card, CFG, NOW + 1)['percent'] == 73
        assert renders._frozen_context_progress(led.db, {**card, 'card_key': 'synthetic-new-key'}, CFG, NOW + 2)['percent'] is None
        assert renders._frozen_context_progress(led.db, {**card, 'channel_id': 'synthetic-new-channel'}, CFG, NOW + 2)['percent'] is None
    assert len(led.artifacts('patient_context_progress_snapshot_v1')) == 1


def test_old_queued_ocr_plan_is_unchanged_until_an_independent_render_reason(led, monkeypatch):
    from notify_testkit import _attach
    from test_ocr_delivery import _save
    _seed_thread(led, mids=(100,))
    _save(led, _attach(led, 100))
    original = renders._context_progress_header
    monkeypatch.setattr(renders, '_context_progress_header', lambda db, card, containers, cfg, now: containers)
    _dispatch(led, _intent(led, payload={'message_ids': [100]}))
    old = dict(_latest_render(led))
    assert json.loads(old['spec_json'])['parts']['processing_display_version'] == 2
    monkeypatch.setattr(renders, '_context_progress_header', original)
    monkeypatch.setattr(progress, 'read', lambda *args: pytest.fail('queued old plan must not capture'))
    notify_cards.sweep(led, CFG, now=NOW + 1)
    assert dict(_latest_render(led)) == old
    assert not led.artifacts('patient_context_progress_snapshot_v1')
