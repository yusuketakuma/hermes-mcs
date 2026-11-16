"""Synthetic regression: minimal previews stay minimal on invalid content hashes."""
import pytest

import notify_render
from notify_testkit import _msg, _patient, led as led


@pytest.mark.parametrize('content_hash', [None, '', 'invalid', 'a' * 63, 'g' * 64])
def test_minimal_full_message_with_invalid_hash_has_no_identifying_header(led, content_hash):
    _patient(led, name='合成 花子')
    _msg(led, 100, body='合成の本文', sender='合成職員', org='合成所属')
    led.db.execute('UPDATE messages SET content_hash=? WHERE message_id=100', (content_hash,))
    preview = notify_render.notification_preview(
        led.db, {'kind': 'thread', 'project_id': 1}, {'shown': [100]},
        cfg={'notify': {'preview_privacy': 'minimal'}})
    assert preview == 'MCS: 新しい投稿'


@pytest.mark.parametrize('privacy,patient', [('full', '合成 花子'), ('initial', '合○ 花○')])
def test_nonminimal_invalid_hash_retains_existing_missing_content_notice(led, privacy, patient):
    _patient(led, name='合成 花子')
    _msg(led, 100, org='合成所属')
    led.db.execute("UPDATE messages SET content_hash='invalid' WHERE message_id=100")
    preview = notify_render.notification_preview(
        led.db, {'kind': 'thread', 'project_id': 1}, {'shown': [100]},
        cfg={'notify': {'preview_privacy': privacy}})
    assert patient in preview
    assert '合成所属' in preview
    assert '投稿本文が未取得か確認できない状態です。' in preview


@pytest.mark.parametrize('state', ['unknown', 'deleted'])
def test_minimal_unavailable_body_remains_generic(led, state):
    _patient(led, name='合成 花子')
    _msg(led, 100, org='合成所属')
    led.db.execute('UPDATE messages SET body_state=? WHERE message_id=100', (state,))
    assert notify_render.notification_preview(
        led.db, {'kind': 'thread', 'project_id': 1}, {'shown': [100]},
        cfg={'notify': {'preview_privacy': 'minimal'}}) == 'MCS: 新しい投稿'


def test_minimal_verified_post_keeps_urgency_and_request_without_identifiers(led, monkeypatch):
    _patient(led, name='合成 花子')
    _msg(led, 100, org='合成所属')
    monkeypatch.setattr(notify_render, 'structured_block',
                        lambda *args, **kwargs: {'text': '合成見出し\n・依頼: 合成の確認'})
    monkeypatch.setattr(notify_render.structured_view, 'urgency_qc_suffix', lambda *args: '')
    preview = notify_render.notification_preview(
        led.db, {'kind': 'thread', 'project_id': 1}, {'shown': [100]},
        _urgencies={100: 'llm'}, cfg={'notify': {'preview_privacy': 'minimal'}})
    assert preview == notify_render.URGENCY_PLAIN['llm'] + ' · MCS: 新しい投稿（依頼あり）'
