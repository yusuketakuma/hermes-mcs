"""Synthetic extraction commits preserve history refresh across failures."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

import extract_llm
import extraction_refresh
import notify_cards
from notify_testkit import (CFG, NOW, _begin, _card, _deliver, _dispatch, _extract, _intent,
                            _latest_render, _msg, _receipt, _seed_thread, led, pinned_clock)
from test_patient_history_cards import captured, ready, seed


__all__ = ['led', 'pinned_clock']
pytestmark = pytest.mark.usefixtures('pinned_clock')


def _completed_card(led, monkeypatch):
    event = seed(led, 1)
    captured(led)
    ready(monkeypatch)
    _dispatch(led, event)
    render = _latest_render(led)
    assert _begin(led, render)['granted']
    assert _receipt(led, render, '0000000000000001', message_id='stable')['applied']
    assert json.loads(_card(led)['ui_state'])['history_latest']['phase'] == 'complete'
    return render


def _claimed(led, monkeypatch):
    monkeypatch.setattr(extract_llm.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(extract_llm, '_WRITER_STATE', (False, 1))
    monkeypatch.setattr(extraction_refresh, 'load_config', lambda: CFG)
    row = dict(led.db.execute('SELECT * FROM messages WHERE project_id=1 AND message_id=1').fetchone())
    assert extract_llm._claim(led, row) is not None
    extract_llm._capture_checkpoint_state(led.db, row, None)
    return row


def _commit(led, row):
    return extract_llm._commit_extraction_atomic(
        led, row, '{"summary":"合成の結果"}', None, None, None, None, (), check_current=False)


def test_failed_real_refresh_keeps_dirty_history_and_sweep_recovers(led, monkeypatch):
    before = _completed_card(led, monkeypatch)
    row = _claimed(led, monkeypatch)
    original = notify_cards.rerender_message_cards

    def broken(*args, **kwargs):
        raise RuntimeError('合成の例外')

    monkeypatch.setattr(notify_cards, 'rerender_message_cards', broken)
    assert _commit(led, row) == 'written'
    assert extract_llm._current(led, 1, row['content_hash'])
    ui = json.loads(_card(led)['ui_state'])
    assert ui['history_source_dirty'] is True and ui['history_dirty_since'] is not None
    assert _latest_render(led)['delivery_id'] == before['delivery_id']
    monkeypatch.setattr(notify_cards, 'rerender_message_cards', original)
    ready(monkeypatch, False)
    notify_cards.sweep(led, CFG, now=NOW + 301)
    ui = json.loads(_card(led)['ui_state'])
    assert ui['history_latest']['phase'] == 'analyzing'
    assert not ui.get('history_source_dirty')
    assert _latest_render(led)['render_rev'] > before['render_rev']


def test_dirty_marker_tampering_rolls_back_extraction(led, monkeypatch):
    _completed_card(led, monkeypatch)
    row = _claimed(led, monkeypatch)
    before = _card(led)['ui_state']
    led.db.execute("CREATE TRIGGER synthetic_tamper_history AFTER UPDATE ON notification_cards "
                   "WHEN NEW.kind='patient_history' BEGIN "
                   "UPDATE notification_cards SET ui_state='{}' WHERE card_id=NEW.card_id; END")
    led.db.commit()
    with pytest.raises(RuntimeError, match='history_refresh_unverified'):
        _commit(led, row)
    assert not led.db.in_transaction
    assert _card(led)['ui_state'] == before
    assert not extract_llm._current(led, 1, row['content_hash'])


def test_invalid_history_state_is_not_overwritten(led, monkeypatch):
    _completed_card(led, monkeypatch)
    led.db.execute("UPDATE notification_cards SET ui_state='{bad json' WHERE kind='patient_history'")
    led.db.commit()
    row = _claimed(led, monkeypatch)
    with pytest.raises(RuntimeError, match='history_refresh_invalid_state'):
        _commit(led, row)
    assert _card(led)['ui_state'] == '{bad json'
    assert not extract_llm._current(led, 1, row['content_hash'])


def test_checkpoint_insert_tampering_rolls_back(led, monkeypatch):
    _completed_card(led, monkeypatch)
    row = _claimed(led, monkeypatch)
    led.db.execute("CREATE TRIGGER synthetic_tamper_chunk AFTER INSERT ON artifacts "
                   "WHEN NEW.kind='extract_llm_chunk' BEGIN "
                   "UPDATE artifacts SET content='{}' WHERE artifact_id=NEW.artifact_id; END")
    led.db.commit()
    before = _card(led)['ui_state']
    with pytest.raises(RuntimeError, match='extract_insert_unverified'):
        extract_llm._persist_chunks(led, row, {0: {'summary': '合成の候補'}})
    assert not led.db.execute("SELECT 1 FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()
    assert _card(led)['ui_state'] == before


@pytest.mark.parametrize('error', [sqlite3.OperationalError('private'), OSError('private')])
def test_postcommit_renderer_failure_does_not_hide_commit(monkeypatch, capsys, error):
    db = sqlite3.connect(':memory:')
    monkeypatch.setattr(extract_llm.semantic_store, 'refresh_cards_allowed', lambda *a: True)
    monkeypatch.setattr(extract_llm, 'load_config', lambda: CFG)
    def broken(*args, **kwargs):
        raise error
    monkeypatch.setattr(extraction_refresh, 'refresh_extraction_cards', broken)
    try:
        extract_llm._rerender_cards(SimpleNamespace(db=db), 1, 1)
        message = capsys.readouterr().err
        assert 'card_rerender_failed' in message and 'private' not in message
    finally:
        db.close()


def test_notification_free_reprocessing_does_not_dirty_history(led, monkeypatch):
    _completed_card(led, monkeypatch)
    led.job_add('semantic', 1, message_id=1,
                payload={'reprocess_only': True, 'notification_free': True})
    row = _claimed(led, monkeypatch)
    before = _card(led)['ui_state']
    assert _commit(led, row) == 'written'
    assert _card(led)['ui_state'] == before


def test_unrelated_extraction_is_not_suppressed_by_quiet_job_flag(led):
    _seed_thread(led, mids=(100,))
    _extract(led, 100, {'summary': '合成の旧要約'}, kind='extract_llm')
    _dispatch(led, _intent(led, payload={'message_ids': [100]}))
    _deliver(led)
    before = _latest_render(led)
    assert before['state'] == 'delivered'
    source_hash = led.db.execute('SELECT content_hash FROM messages WHERE message_id=100').fetchone()[0]
    old_artifact = led.db.execute("SELECT MAX(artifact_id) FROM artifacts WHERE kind='extract_llm' "
                                  "AND message_id=100").fetchone()[0]
    led.job_add('semantic', 1, message_id=100,
                payload={'reprocess_only': True, 'notification_free': True})
    # A direct artifact write is not evidence that the quiet job produced it.
    _extract(led, 100, {'summary': '合成の通常更新'}, kind='extract_llm')
    assert led.db.execute('SELECT content_hash FROM messages WHERE message_id=100').fetchone()[0] == source_hash
    assert led.db.execute("SELECT MAX(artifact_id) FROM artifacts WHERE kind='extract_llm' "
                          "AND message_id=100").fetchone()[0] > old_artifact
    job = led.db.execute("SELECT payload FROM fetch_jobs WHERE kind='semantic' AND message_id=100").fetchone()
    assert json.loads(job['payload'])['notification_free'] is True
    sweep = notify_cards.sweep(led, CFG, now=NOW + 301)
    after = _latest_render(led)
    assert sweep['updated'] == 1
    assert after['render_rev'] == before['render_rev'] + 1
    assert after['delivery_id'] != before['delivery_id']
    assert after['op'] == 'update' and after['state'] == 'queued' and after['spec_published'] == 1


def test_notification_free_reprocessing_keeps_new_reply_intent(led):
    _seed_thread(led, mids=(100,))
    _extract(led, 100, {'summary': '合成の旧要約'}, kind='extract_llm')
    _dispatch(led, _intent(led, payload={'message_ids': [100]}))
    _deliver(led)
    led.job_add('semantic', 1, message_id=100,
                payload={'reprocess_only': True, 'notification_free': True})
    _extract(led, 100, {'summary': '合成の再解析要約'}, kind='extract_llm')
    _msg(led, 101, parent=100, body='合成の新着返信')
    event = _intent(led, payload={'message_ids': [101]})
    _dispatch(led, event, now=NOW + 301)
    assert led.db.execute("SELECT 1 FROM notification_intent_cards i "
                          "JOIN notification_renders r ON r.delivery_id=i.delivery_id "
                          "WHERE i.event_id=? AND i.state='pending' AND r.state='queued'",
                          (event['event_id'],)).fetchone()


def test_notification_free_reprocessing_does_not_hide_source_edit(led):
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={'message_ids': [100]}))
    _deliver(led)
    before = _latest_render(led)['render_rev']
    led.job_add('semantic', 1, message_id=100,
                payload={'reprocess_only': True, 'notification_free': True})
    with led.db:
        led.db.execute("UPDATE messages SET content_hash=?,body_text=? WHERE message_id=100",
                       ('a' * 64, '合成の訂正済み本文'))
    notify_cards.sweep(led, CFG, now=NOW + 301)
    assert _latest_render(led)['render_rev'] > before


def test_refresh_does_not_swallow_or_rollback_renderer_owned_transaction(monkeypatch):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE sample(value TEXT)')

    def broken(ledger, *_args, **_kwargs):
        ledger.db.execute("INSERT INTO sample VALUES ('合成値')")
        raise RuntimeError('synthetic')

    monkeypatch.setattr(notify_cards, 'rerender_message_cards', broken)
    try:
        with pytest.raises(RuntimeError, match='synthetic'):
            extraction_refresh.refresh_extraction_cards(SimpleNamespace(db=db), 1, 1, cfg=CFG)
        assert db.in_transaction and db.execute('SELECT count(*) FROM sample').fetchone()[0] == 1
        db.rollback()
        assert db.execute('SELECT count(*) FROM sample').fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize('error', [sqlite3.DatabaseError('synthetic'), OSError('synthetic')])
def test_refresh_does_not_swallow_storage_failure(monkeypatch, error):
    db = sqlite3.connect(':memory:')
    def broken(*args, **kwargs):
        raise error
    monkeypatch.setattr(notify_cards, 'rerender_message_cards', broken)
    try:
        with pytest.raises(type(error), match='synthetic'):
            extraction_refresh.refresh_extraction_cards(SimpleNamespace(db=db), 1, 1, cfg=CFG)
        assert not db.in_transaction
    finally:
        db.close()
