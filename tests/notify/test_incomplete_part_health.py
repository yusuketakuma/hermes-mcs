"""Synthetic live-part health keeps accepted intents separate from delivery proof."""
import json
import sqlite3

import pytest

import health_report
import health_watch
import notify_scope
from ingest_testkit import _ledger


NOW = 200_000.0
SLACK = {'notify': {'interactive': 'slack'}}


@pytest.fixture
def stored(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(health_report.time, 'time', lambda: NOW)
    monkeypatch.setattr(health_report, 'HEALTH_FILE', str(tmp_path / 'absent.json'))
    monkeypatch.setattr(health_report, 'attachment_free_mb', lambda path=None: 10_000)
    monkeypatch.setattr(health_report.maintenance, 'local_backup_status',
                        lambda **_: {'state': 'verified', 'newest_age_s': 50, 'observed_at': NOW})
    try:
        yield ledger.db
    finally:
        ledger.close()


def _render(db, states, *, transport='slack', card=None, revision=1, render_state='delivered'):
    if card is None:
        key = f'synthetic-{db.execute("SELECT COUNT(*) FROM notification_cards").fetchone()[0]}'
        card = db.execute(
            "INSERT INTO notification_cards(card_key,kind,anchor_key,transport,delivery_state,created_at,updated_at) "
            "VALUES(?,'thread','synthetic',?,'delivered',?,?)",
            (key, transport, NOW - 100, NOW)).lastrowid
    delivery = f'synthetic-{card}-{revision}'
    db.execute(
        "INSERT INTO notification_renders(delivery_id,card_id,op,render_rev,route_epoch,transport,"
        "payload_hash,correlation,state,created_at,updated_at) VALUES(?,?,'update',?,1,?,?,?, ?,?,?)",
        (delivery, card, revision, transport, 'synthetic-hash', delivery, render_state, NOW - 100, NOW))
    for index, (kind, state, error) in enumerate(states):
        db.execute(
            "INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,state,error_code,name,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)", (delivery, f'part-{index}', kind, index, state, error,
                                       'SYNTHETIC_PRIVATE_ATTACHMENT', NOW - 60))
    return card, delivery


@pytest.mark.parametrize('state', ['unknown', 'pending', 'not_sent', 'held'])
def test_accepted_outbox_does_not_hide_incomplete_delivered_card(stored, tmp_path, state):
    _render(stored, [('attachment_part', state, 'synthetic_failure')])
    stored.commit()
    before = stored.total_changes
    report = health_report.build_health(stored, {'errors': [], 'notify': {}}, 'ok', cfg=SLACK)
    assert stored.total_changes == before
    assert report['notify']['pending'] == report['notify']['pending_by_reason']['total'] == 0
    assert report['notify']['state'] == 'incomplete' and report['overall'] == 'degraded'
    assert 'notification_parts_incomplete' in report['state_reasons']
    parts = report['notify']['incomplete_parts']
    assert parts['total'] == parts['cards'] == 1 and parts['by_state'][state] == 1
    assert parts['oldest_age_s'] == 60
    path = tmp_path / 'health.json'
    path.write_text(json.dumps(report))
    watched = health_watch.classify_health(str(path), NOW + 1, 1800)
    assert watched['status'] == 'degraded' and watched['notify_incomplete_parts'] == 1
    text = '\n'.join(health_watch._alert_lines({**watched, 'alert': True, 'disk_alert': False}))
    assert '本文・添付などの送信完了を確認できません 1件' in text
    assert '自動で再送しません' in text and 'SYNTHETIC_PRIVATE_ATTACHMENT' not in text
    assert 'notification_parts_incomplete' in health_watch.TRANSIENT_REASONS


def test_latest_active_render_only_and_unavailable_source_is_separate(stored):
    card, _ = _render(stored, [('body_part', 'unknown', None)])
    _render(stored, [('body_part', 'delivered', None)], card=card, revision=2)
    _render(stored, [('body_part', 'unknown', None)], transport='discord')
    revoked, _ = _render(stored, [('body_part', 'unknown', None)])
    stored.execute("UPDATE notification_cards SET revoked_at=?,delivery_state='revoked' WHERE card_id=?",
                   (NOW, revoked))
    _render(stored, [('attachment_part', 'not_sent', 'attachment_unavailable')])
    _render(stored, [('body_part', 'unknown', None)], render_state='cancelled')
    stored.commit()
    before = stored.total_changes
    report = notify_scope.incomplete_parts_health(stored, NOW, SLACK)
    assert report['total'] == report['cards'] == 0 and report['oldest_age_s'] is None
    assert not any(report['by_state'].values()) and stored.total_changes == before
    assert notify_scope.incomplete_parts_health(stored, NOW)['total'] == 1


def test_mixed_parts_count_cards_once_and_preserve_borrowed_transaction(stored):
    _render(stored, [('body_part', 'pending', None), ('attachment_part', 'unknown', None),
                     ('body_part', 'not_sent', 'wire_too_long'), ('thread', 'held', None),
                     ('card', 'unknown', None)])
    assert stored.in_transaction
    before = stored.total_changes
    report = notify_scope.incomplete_parts_health(stored, NOW, SLACK)
    assert report['total'] == 4 and report['cards'] == 1
    assert report['by_state'] == dict.fromkeys(('pending', 'unknown', 'not_sent', 'held'), 1)
    assert stored.in_transaction and stored.total_changes == before
    stored.rollback()
    assert notify_scope.incomplete_parts_health(stored, NOW, SLACK)['total'] == 0


@pytest.mark.parametrize('new_state', ['queued', 'sending', 'cancelled', 'unknown'])
def test_unconfirmed_refresh_cannot_hide_the_last_delivered_cards_parts(stored, new_state):
    card, _ = _render(stored, [('attachment_part', 'unknown', None)])
    _render(stored, [('attachment_part', 'pending', None)], card=card, revision=2,
            render_state=new_state)
    assert notify_scope.incomplete_parts_health(stored, NOW, SLACK)['by_state']['unknown'] == 1
    _render(stored, [('attachment_part', 'delivered', None)], card=card, revision=3)
    assert notify_scope.incomplete_parts_health(stored, NOW, SLACK)['total'] == 0


def test_missing_delivery_evidence_is_not_reported_as_zero(stored):
    stored.execute('DROP TABLE notification_render_parts')
    with pytest.raises(sqlite3.Error):
        notify_scope.incomplete_parts_health(stored, NOW, SLACK)


@pytest.mark.parametrize('parts', [
    {'total': True, 'by_state': {'pending': 1, 'unknown': 0, 'not_sent': 0, 'held': 0}},
    {'total': 0, 'by_state': {'pending': 0, 'unknown': 1, 'not_sent': 0, 'held': 0}},
    {'total': 1, 'by_state': {'pending': 0, 'unknown': 1, 'not_sent': -1, 'held': 1}},
    {'total': 1, 'by_state': {'pending': 0, 'unknown': 1, 'not_sent': 0, 'held': 0, 'private': 0}},
])
def test_malformed_part_counts_remain_unknown(tmp_path, parts):
    path = tmp_path / 'health.json'
    path.write_text(json.dumps({'overall': 'degraded', 'at': NOW,
                               'state_reasons': ['notification_parts_incomplete'],
                               'notify': {'incomplete_parts': parts}}))
    report = health_watch.classify_health(str(path), NOW + 1, 1800)
    assert report['notify_incomplete_parts'] is None and report['status'] == 'degraded'
