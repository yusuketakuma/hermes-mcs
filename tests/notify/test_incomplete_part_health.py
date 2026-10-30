"""Synthetic live-part health keeps accepted intents separate from delivery proof."""
import hashlib
import json
import sqlite3

import pytest

import health_report
import health_watch
import notify_scope
from ingest_testkit import _ledger
from notify_testkit import led as led


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
    scope = ('synthetic-profile', 'synthetic-application',
             'synthetic-guild' if transport == 'discord' else None,
             None if transport == 'discord' else 'synthetic-team', 'synthetic-channel')
    if card is None:
        key = f'synthetic-{db.execute("SELECT COUNT(*) FROM notification_cards").fetchone()[0]}'
        card = db.execute(
            "INSERT INTO notification_cards(card_key,kind,anchor_key,transport,delivery_state,"
            "profile,application_id,guild_id,team_id,channel_id,created_at,updated_at) "
            "VALUES(?,'thread','synthetic',?,'delivered',?,?,?,?,?,?,?)",
            (key, transport, *scope, NOW - 100, NOW)).lastrowid
    delivery = f'synthetic-{card}-{revision}'
    db.execute(
        "INSERT INTO notification_renders(delivery_id,card_id,op,render_rev,route_epoch,transport,"
        "payload_hash,correlation,state,profile,application_id,guild_id,team_id,channel_id,created_at,updated_at) "
        "VALUES(?,?,'update',?,1,?,?,?,?,?,?,?,?,?,?,?)",
        (delivery, card, revision, transport, 'synthetic-hash', delivery, render_state, *scope, NOW - 100, NOW))
    for index, (kind, state, error) in enumerate(states):
        db.execute(
            "INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,state,error_code,name,"
            "payload_sha256,attachment_id,remote_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (delivery, f'part-{index}', kind, index, state, error,
             f'm:100#{index + 1}' if kind == 'body_part' else 'SYNTHETIC_PRIVATE_ATTACHMENT',
             f'{index + 1:064x}', index + 1 if kind == 'attachment_part' else None,
             f'synthetic-remote-{delivery}-{index}' if state == 'delivered' else None, NOW - 60))
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


def test_retired_revoked_and_cancelled_are_separate_from_undisclosed_unavailable(stored):
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
    assert report['total'] == report['cards'] == 1 and report['oldest_age_s'] == 60
    assert report['by_state']['not_sent'] == 1 and stored.total_changes == before
    assert notify_scope.incomplete_parts_health(stored, NOW)['total'] == 2


@pytest.mark.parametrize('kind', ['body_part', 'attachment_part', 'thread'])
@pytest.mark.parametrize('state', ['pending', 'unknown', 'not_sent', 'held'])
def test_card_only_refresh_cannot_hide_an_old_unfinished_part(stored, kind, state):
    card, delivery = _render(stored, [(kind, state, 'synthetic_failure')])
    _render(stored, [('card', 'delivered', None)], card=card, revision=2)
    before = stored.total_changes
    report = notify_scope.incomplete_parts_health(stored, NOW, SLACK)
    assert report['total'] == report['cards'] == 1
    assert report['by_state'][state] == 1 and report['oldest_age_s'] == 60
    assert stored.total_changes == before
    assert stored.execute('SELECT state FROM notification_render_parts WHERE delivery_id=?',
                          (delivery,)).fetchone()[0] == state


@pytest.mark.parametrize(('kind', 'difference', 'resolved'), [
    ('body_part', None, True),
    ('body_part', 'name', False),
    ('body_part', 'hash', False),
    ('body_part', 'unnamed', False),
    ('body_part', 'position', True),
    ('attachment_part', None, True),
    ('attachment_part', 'attachment', False),
    ('attachment_part', 'hash', False),
    ('thread', None, True),
    ('thread', 'hash', False),
    ('thread', 'position', False),
    ('body_part', 'scope', False),
    ('attachment_part', 'scope', False),
    ('thread', 'scope', False),
    ('body_part', 'remote', False),
    ('attachment_part', 'remote', False),
    ('thread', 'remote', False),
])
def test_later_delivery_resolves_only_the_same_part(stored, kind, difference, resolved):
    card, old = _render(stored, [(kind, 'unknown', None)])
    _, new = _render(stored, [(kind, 'delivered', None)], card=card, revision=2)
    if difference == 'name':
        stored.execute("UPDATE notification_render_parts SET name='m:101#1' WHERE delivery_id=?", (new,))
    elif difference == 'hash':
        stored.execute('UPDATE notification_render_parts SET payload_sha256=? WHERE delivery_id=?',
                       ('a' * 64, new))
    elif difference == 'unnamed':
        stored.execute('UPDATE notification_render_parts SET name=NULL')
    elif difference == 'position':
        stored.execute("UPDATE notification_render_parts SET part_id='part-99' WHERE delivery_id=?", (new,))
    elif difference == 'attachment':
        stored.execute('UPDATE notification_render_parts SET attachment_id=99 WHERE delivery_id=?', (new,))
    elif difference == 'scope':
        stored.execute("UPDATE notification_renders SET channel_id='SYNTHETIC_OTHER' WHERE delivery_id=?", (new,))
    elif difference == 'remote':
        stored.execute("UPDATE notification_render_parts SET remote_id='' WHERE delivery_id=?", (new,))
    before = stored.total_changes
    report = notify_scope.incomplete_parts_health(stored, NOW, SLACK)
    assert report['total'] == report['cards'] == int(not resolved)
    assert stored.total_changes == before
    assert stored.execute('SELECT state FROM notification_render_parts WHERE delivery_id=?', (old,)).fetchone()[0] == 'unknown'


@pytest.mark.parametrize('later_hash', [None, 'b' * 64])
def test_uncaptioned_unavailable_is_incomplete_until_its_later_receipt(stored, later_hash):
    card, old = _render(stored, [('attachment_part', 'not_sent', 'attachment_unavailable')])
    stored.execute('UPDATE notification_render_parts SET payload_sha256=NULL WHERE delivery_id=?', (old,))
    assert notify_scope.incomplete_parts_health(stored, NOW, SLACK)['total'] == 1
    _, new = _render(stored, [('attachment_part', 'pending', None)], card=card, revision=2)
    stored.execute('UPDATE notification_render_parts SET payload_sha256=? WHERE delivery_id=?', (later_hash, new))
    assert notify_scope.incomplete_parts_health(stored, NOW, SLACK)['by_state']['not_sent'] == 1
    stored.execute("UPDATE notification_render_parts SET state='delivered',remote_id='SYNTHETIC_CAPTION' "
                   'WHERE delivery_id=?', (new,))
    assert notify_scope.incomplete_parts_health(stored, NOW, SLACK)['total'] == 0


@pytest.mark.parametrize('kind', ['body_part', 'attachment_part', 'thread'])
def test_one_matching_delivery_resolves_older_claims_without_settling_them(stored, kind):
    card, _ = _render(stored, [(kind, 'unknown', None)])
    _render(stored, [(kind, 'not_sent', 'synthetic_failure')], card=card, revision=2)
    _render(stored, [(kind, 'held', None)], card=card, revision=3)
    assert notify_scope.incomplete_parts_health(stored, NOW, SLACK)['total'] == 3
    _render(stored, [(kind, 'delivered', None)], card=card, revision=4)
    before = stored.total_changes
    assert notify_scope.incomplete_parts_health(stored, NOW, SLACK)['total'] == 0
    assert stored.total_changes == before
    assert [row[0] for row in stored.execute(
        'SELECT p.state FROM notification_render_parts p JOIN notification_renders r USING(delivery_id) '
        'WHERE r.card_id=? ORDER BY r.render_rev', (card,))] == ['unknown', 'not_sent', 'held', 'delivered']


@pytest.mark.parametrize('field', ['profile', 'application_id', 'guild_id', 'team_id', 'channel_id', 'transport'])
def test_other_delivery_scope_cannot_resolve_an_old_part(stored, field):
    card, _ = _render(stored, [('body_part', 'unknown', None)])
    _, new = _render(stored, [('body_part', 'delivered', None)], card=card, revision=2)
    stored.execute(f'UPDATE notification_renders SET {field}=? WHERE delivery_id=?',
                   ('discord' if field == 'transport' else 'SYNTHETIC_OTHER', new))
    assert notify_scope.incomplete_parts_health(stored, NOW, SLACK)['total'] == 1


@pytest.mark.parametrize('transport', ['discord', 'slack', 'lineworks'])
@pytest.mark.parametrize('field', ['profile', 'application_id', 'organization', 'channel_id'])
@pytest.mark.parametrize('invalid', [None, '', ' \t\r\n', '\u3000', b'synthetic-scope'])
def test_matching_malformed_scope_cannot_resolve_an_old_part(stored, transport, field, invalid):
    field = ('guild_id' if transport == 'discord' else 'team_id') if field == 'organization' else field
    card, old = _render(stored, [('body_part', 'unknown', None)], transport=transport)
    _render(stored, [('body_part', 'delivered', None)], transport=transport, card=card, revision=2)
    stored.execute(f'UPDATE notification_renders SET {field}=? WHERE card_id=?', (invalid, card))
    before = stored.total_changes
    report = notify_scope.incomplete_parts_health(stored, NOW)
    assert report['total'] == report['cards'] == 1
    assert report['by_state']['unknown'] == 1 and stored.total_changes == before
    assert set(report) == {'total', 'cards', 'by_state', 'oldest_age_s', 'observed_at'}
    assert stored.execute('SELECT state FROM notification_render_parts WHERE delivery_id=?', (old,)).fetchone()[0] == 'unknown'


@pytest.mark.parametrize('transport', ['discord', 'slack', 'lineworks'])
@pytest.mark.parametrize('kind', ['body_part', 'attachment_part', 'thread'])
def test_complete_scope_with_unused_null_column_resolves_only_the_matching_part(stored, transport, kind):
    card, old = _render(stored, [(kind, 'held', None)], transport=transport)
    _render(stored, [(kind, 'delivered', None)], transport=transport, card=card, revision=2)
    unused = 'team_id' if transport == 'discord' else 'guild_id'
    assert stored.execute(f'SELECT {unused} FROM notification_renders WHERE delivery_id=?', (old,)).fetchone()[0] is None
    before = stored.total_changes
    assert notify_scope.incomplete_parts_health(stored, NOW)['total'] == 0
    assert stored.total_changes == before
    assert stored.execute('SELECT state FROM notification_render_parts WHERE delivery_id=?', (old,)).fetchone()[0] == 'held'


def test_unrecognized_transport_cannot_supply_later_scope_proof(stored):
    card, _ = _render(stored, [('body_part', 'unknown', None)], transport='synthetic-unknown')
    _render(stored, [('body_part', 'delivered', None)], transport='synthetic-unknown', card=card, revision=2)
    assert notify_scope.incomplete_parts_health(stored, NOW)['total'] == 1


def test_earlier_or_unconfirmed_delivery_and_text_acceptance_are_not_later_part_proof(stored):
    card, _ = _render(stored, [('body_part', 'delivered', None)])
    _render(stored, [('body_part', 'unknown', None)], card=card, revision=2)
    _render(stored, [('body_part', 'delivered', None)], card=card, revision=3, render_state='queued')
    _render(stored, [('body_part', 'delivered', None)])  # Another card cannot stand in for this one.
    _render(stored, [('attachment_part', 'held', None)])
    stored.execute("INSERT INTO notify_outbox(kind,project_id,payload,state,route,created_at,updated_at) "
                   "VALUES('attachment_followup',1,?,'accepted','text',?,?)",
                   (json.dumps({'attachment_id': 1, 'message_id': 100}), NOW, NOW))
    assert notify_scope.incomplete_parts_health(stored, NOW, SLACK)['total'] == 2


@pytest.mark.parametrize(('manifest', 'expected'), [
    ({'parts': {'manifest': [{'part_id': 'part-0', 'attachment_id': 1, 'unavailable': True}]}}, 0),
    ({'parts': {'manifest': [{'part_id': 'part-0', 'attachment_id': 1, 'unavailable': False}]}}, 1),
    ({'parts': {'manifest': [{'part_id': 'part-0', 'attachment_id': 1, 'unavailable': 1}]}}, 1),
    ({'parts': {'manifest': [{'part_id': 'part-0', 'attachment_id': True, 'unavailable': True}]}}, 1),
    ({'parts': {'manifest': [{'part_id': 'other-part', 'attachment_id': 1, 'unavailable': True}]}}, 1),
    ({'parts': {'manifest': ['synthetic non-object entry']}}, 1),
    (None, 1),
])
def test_hashless_held_attachment_requires_its_sealed_unavailable_identity(stored, manifest, expected):
    card, old = _render(stored, [('attachment_part', 'held', None)])
    _, new = _render(stored, [('attachment_part', 'delivered', None)], card=card, revision=2)
    stored.execute('UPDATE notification_render_parts SET payload_sha256=NULL')
    stored.execute('UPDATE notification_renders SET spec_json=? WHERE delivery_id=?',
                   (json.dumps(manifest) if manifest is not None else '{bad', old))
    assert notify_scope.incomplete_parts_health(stored, NOW, SLACK)['total'] == expected
    assert stored.execute('SELECT remote_id FROM notification_render_parts WHERE delivery_id=?', (new,)).fetchone()[0]


@pytest.mark.parametrize('caption', [None, 'Synthetic attachment unavailable'])
def test_unavailable_seed_is_complete_only_after_its_caption_delivery(stored, caption):
    import notify_parts
    import notify_transport

    _, delivery = _render(stored, [], transport='discord')
    part = {'part_id': 'attach:0001', 'kind': 'attachment_part', 'index': 0,
            'attachment_id': 1, 'name': 'synthetic.bin', 'unavailable': True}
    if caption:
        part['caption'] = caption
    notify_parts.seed_parts(stored, {'delivery_id': delivery, 'parts': {'manifest': [part]}}, NOW)
    assert notify_scope.incomplete_parts_health(stored, NOW)['total'] == 1
    row = stored.execute('SELECT state,error_code FROM notification_render_parts WHERE delivery_id=?', (delivery,)).fetchone()
    assert tuple(row) == (('pending', None) if caption else ('not_sent', 'attachment_unavailable'))
    if caption:
        with stored:
            result = notify_transport.settle_part_receipt(stored, {
                'delivery_id': delivery, 'part_id': part['part_id'], 'render_rev': 1,
                'payload_hash': 'synthetic-hash', 'route_epoch': 1, 'correlation': delivery,
                **notify_scope.stored_scope(notify_scope.render_row(stored, delivery)),
                'result': 'delivered', 'remote_id': 'SYNTHETIC_CAPTION',
            }, NOW)
        assert result['applied']
        assert notify_scope.incomplete_parts_health(stored, NOW)['total'] == 0


def test_real_failed_thread_then_card_only_delivery_keeps_the_old_plan_incomplete(led, tmp_path):
    import notify_cards
    from notify_testkit import (CFG, NOW as DELIVERY_NOW, _attach, _begin, _dispatch, _intent,
                               _latest_render, _part_receipt, _receipt, _seed_thread)

    _seed_thread(led, mids=(100,))
    path = tmp_path / 'synthetic.bin'
    blob = b'synthetic attachment'
    path.write_bytes(blob)
    _attach(led, 100, local_path=str(path), sha256=hashlib.sha256(blob).hexdigest(), nbytes=len(blob))
    assert _dispatch(led, _intent(led, payload={'message_ids': [100]}))['dispatched']
    first = _latest_render(led)
    assert _begin(led, first)['granted']
    assert _receipt(led, first, f'{1:016x}', message_id='synthetic-card')['applied']
    assert _part_receipt(led, first, 'thread', result='not_sent', remote_id=None,
                         error_code='http_403')['applied']
    old_parts = [dict(row) for row in led.db.execute(
        "SELECT kind,state FROM notification_render_parts WHERE delivery_id=? AND kind!='card'",
        (first['delivery_id'],))]
    assert {p['kind'] for p in old_parts} >= {'thread', 'body_part', 'attachment_part'}
    assert {p['state'] for p in old_parts} == {'not_sent', 'held'}
    with led.db:
        assert notify_cards.issue_render(led.db, first['card_id'], CFG, DELIVERY_NOW + 1, [], force=True)
    later = _latest_render(led)
    assert later['render_rev'] > first['render_rev']
    assert [row[0] for row in led.db.execute(
        'SELECT kind FROM notification_render_parts WHERE delivery_id=?', (later['delivery_id'],))] == ['card']
    assert _begin(led, later, n=2)['granted']
    assert _receipt(led, later, f'{2:016x}', message_id='synthetic-card', n=10)['applied']
    before = led.db.total_changes
    report = notify_scope.incomplete_parts_health(led.db, DELIVERY_NOW + 2, CFG)
    assert report['total'] == len(old_parts) and report['cards'] == 1
    assert report['by_state']['held'] == len(old_parts) - 1
    assert led.db.total_changes == before


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
