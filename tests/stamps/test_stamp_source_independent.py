"""Stamp intent follows an exact post while source text and interpretation change."""
from dataclasses import replace
import json

import pytest

import stamp_write
from test_stamp_write import (
    NOW, _apply, _command, _drain, _form, _press, _press_buttons, _receipt,
    led, pinned_clock, scene,
)

__all__ = ['led', 'pinned_clock', 'scene']
pytestmark = pytest.mark.usefixtures('pinned_clock')


def _update_source(scene):
    ledger, _, _, api, _ = scene
    api.changed_body = True
    for mid in (100, 101):
        ledger.save_messages([api.message(mid)])
    with ledger.db:
        ledger.db.execute('UPDATE notification_cards SET source_generation=source_generation+1')
    return ledger


@pytest.mark.parametrize('when', ['before_admission', 'queued', 'confirmed', 'unknown'])
def test_text_and_analysis_updates_never_cancel_retarget_or_resend(scene, when):
    request = _command(scene, mid=101)
    original_hash = request['source_hash']
    if when != 'before_admission':
        assert _apply(scene, request)['stamp_state'] == 'queued'
    if when == 'confirmed':
        assert _drain(scene)['confirmed'] == 1
    elif when == 'unknown':
        scene[3].failure = TimeoutError('synthetic ambiguous transport')
        assert _drain(scene)['unknown'] == 1
        scene[3].failure = None
    ledger = _update_source(scene)
    assert _form(scene)['form_hash'] == request['form_hash']
    if when == 'before_admission':
        assert _apply(scene, request)['stamp_state'] == 'queued'
    counts = _drain(scene)
    if when == 'unknown':
        assert counts['unknown'] == 1
        assert _receipt(scene, request)['stamp_state'] == 'unknown'
        scene[3].own = 'viewed'
        assert _drain(scene)['confirmed'] == 1
    else:
        assert _receipt(scene, request)['stamp_state'] == 'confirmed'
    assert scene[3].posts == [(101, 'viewed', False)]
    assert scene[3].own == 'viewed'
    _drain(scene)
    assert scene[3].posts == [(101, 'viewed', False)]
    saved = ledger.db.execute('SELECT content FROM artifacts WHERE kind=?', (stamp_write.KIND,)).fetchone()
    assert json.loads(saved[0])['request']['source_hash'] == original_hash


def test_old_stamp_button_uses_its_original_manifest_after_analysis_changes(scene):
    button = next(button for button in _press_buttons(scene) if button['label'].startswith('👀'))
    _update_source(scene)
    assert _press(scene, '👀', button=button)['outcome'] == 'applied'
    assert _drain(scene)['confirmed'] == 1
    assert scene[3].posts == [(101, 'viewed', False)]


@pytest.mark.parametrize('damage', ['deleted', 'parent', 'project', 'root_parent'])
def test_local_post_identity_changes_reject_before_send(scene, damage):
    request = _command(scene, mid=100 if damage == 'root_parent' else 101)
    assert _apply(scene, request)['stamp_state'] == 'queued'
    ledger = scene[0]
    with ledger.db:
        if damage == 'deleted':
            ledger.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=101")
        elif damage == 'project':
            ledger.db.execute("INSERT INTO patients(project_id,patient_name) VALUES(2,'synthetic')")
            ledger.db.execute('UPDATE messages SET project_id=2 WHERE message_id=101')
        else:
            ledger.db.execute('UPDATE messages SET parent_id=999 WHERE message_id=?',
                              (100 if damage == 'root_parent' else 101,))
    assert _drain(scene)['rejected'] == 1
    assert scene[3].posts == []


@pytest.mark.parametrize('damage', [
    {'body_state': 'deleted'}, {'message_id': 999}, {'project_id': 2}, {'parent_id': 999},
])
def test_remote_post_identity_changes_reject_before_send(scene, monkeypatch, damage):
    request = _command(scene, mid=101)
    assert _apply(scene, request)['stamp_state'] == 'queued'
    api = scene[3]
    original = api.message
    monkeypatch.setattr(api, 'message', lambda mid: replace(original(mid), **damage))
    assert _drain(scene)['rejected'] == 1
    assert api.posts == []
    assert _receipt(scene, request)['stamp_error'] == 'stamp_target_invalid'


def test_legacy_form_is_accepted_only_when_its_exact_hash_can_still_be_verified(scene):
    request = _command(scene)
    request['form_hash'] = stamp_write._form_hash(_form(scene), legacy=True)
    assert _apply(scene, request)['stamp_state'] == 'queued'
    assert _drain(scene)['confirmed'] == 1
    second = _command(scene, serial=701, mid=101)
    second['form_hash'] = stamp_write._form_hash(_form(scene), legacy=True)
    _update_source(scene)
    assert _apply(scene, second)['error'] == 'stamp_target_changed'
    assert scene[3].posts == [(100, 'viewed', False)]


@pytest.mark.parametrize('damage', ['form_hash', 'token', 'receipt_source_hash'])
def test_hash_and_receipt_tampering_never_gains_a_send(scene, damage):
    request = _command(scene)
    if damage == 'receipt_source_hash':
        assert _apply(scene, request)['stamp_state'] == 'queued'
        with scene[0].db:
            scene[0].db.execute("UPDATE artifacts SET content=json_set(content,'$.request.source_hash',?) WHERE kind=?",
                               ('f' * 64, stamp_write.KIND))
        assert _drain(scene)['held'] >= 1
    else:
        request[damage] = '0' * (64 if damage == 'form_hash' else 32)
        assert _apply(scene, request)['outcome'] == 'rejected'
    assert scene[3].posts == []


@pytest.mark.parametrize('observation', ['newer', 'older', 'different', 'unknown', 'missing'])
def test_own_reobservation_requires_same_type_and_nondecreasing_time(scene, monkeypatch, observation):
    request = _command(scene, mid=101)
    ledger, _, _, api, _ = scene
    if observation == 'newer':
        monkeypatch.setattr(stamp_write.time, 'time', lambda: NOW + 30)
        api.changed_body = True
        ledger.save_messages([api.message(101)])
        ledger.save_metadata_shadow(api.message(101), publish=True)
        assert _apply(scene, request)['stamp_state'] == 'queued'
        assert _drain(scene)['confirmed'] == 1
        assert api.posts == [(101, 'viewed', False)]
    else:
        if observation == 'older':
            request['expected_own']['observed_at'] += 1
        elif observation == 'different':
            api.own = 'accepted'
            ledger.save_metadata_shadow(api.message(101), publish=True)
        elif observation == 'unknown':
            with ledger.db:
                ledger.db.execute("UPDATE message_metadata SET last_error='reactions_invalid' "
                                  "WHERE message_id=101 AND source='capture'")
        else:
            with ledger.db:
                ledger.db.execute("UPDATE message_metadata SET content=json_set(content,'$.reactions.observed_at',NULL) "
                                  "WHERE message_id=101 AND source='capture'")
        result = _apply(scene, request)
        assert result['outcome'] == 'rejected'
        if observation in ('older', 'different'):
            assert result['error'] == 'stamp_own_changed'
        assert api.posts == []


def test_other_card_actions_still_enforce_the_source_guard(scene):
    from adapters.common import text
    from notify_testkit import _spec, _token_for

    token = _token_for(_spec(scene[0]), 'ack')
    _update_source(scene)
    result = _press(scene, '', button={'token': token})
    assert result['outcome'] == 'rejected' and result['error'] == 'stale_source'
    assert text.ja(result) and text.view_answer(result, lambda pid: True) is None
    assert scene[3].posts == []


def test_changed_remote_own_stamp_has_a_current_refusal_instead_of_legacy_silence(scene):
    from adapters.common import text

    request = _command(scene)
    assert _apply(scene, request)['stamp_state'] == 'queued'
    scene[3].own = 'completed'
    assert _drain(scene)['rejected'] == 1
    result = _receipt(scene, request)
    assert result['stamp_error'] == 'stamp_own_changed'
    assert text.ja(result) == 'スタンプの状態が変わったため、この操作は送信しませんでした。'
    assert text.view_answer(result, lambda pid: True) is None
    assert scene[3].posts == []
