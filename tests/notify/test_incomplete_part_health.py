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

pytestmark = pytest.mark.usefixtures("v1_thread_bodies")


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


def _seal_body_plan(db, delivery, chunks, *, retain):
    from mcs_util import canonical, payload_hash, text_sha256

    render = notify_scope.render_row(db, delivery)
    rows = db.execute('SELECT * FROM notification_render_parts WHERE delivery_id=? ORDER BY idx', (delivery,)).fetchall()
    spec = {'delivery_id': delivery, 'render_rev': render['render_rev'], 'op': render['op'],
            'delivery': {**notify_scope.stored_scope(render), 'route_epoch': render['route_epoch'],
                         'correlation': render['correlation']},
            'parts': {'thread_body_parts': chunks, 'manifest': [
                {'kind': 'body_part', 'part_id': row['part_id'], 'index': row['idx'], 'name': row['name'],
                 'bytes': row['bytes'], 'sha256': text_sha256(chunk)} for row, chunk in zip(rows, chunks, strict=True)]}}
    db.execute('UPDATE notification_renders SET spec_json=?,payload_hash=? WHERE delivery_id=?',
               (canonical(spec).decode() if retain else None, payload_hash(spec), delivery))


def _split_repair(db, *, new_group='m:100', changed=False):
    from mcs_util import text_sha256

    text = 'SYNTHETIC_PRIVATE_BODY\n' + 'あ' * 2100 + '\n' + 'é' * 2300
    original = [text[:2200], text[2200:]]
    current = ('x' + text[1:]) if changed else text
    repaired = [current[start:start + 900] for start in range(0, len(current), 900)]
    card, old = _render(db, [('body_part', 'not_sent', 'msg_too_long')] * len(original))
    _, new = _render(db, [('body_part', 'delivered', None)] * len(repaired), card=card, revision=2)
    for delivery, chunks, group in [(old, original, 'm:100'), (new, repaired, new_group)]:
        for index, chunk in enumerate(chunks):
            db.execute('UPDATE notification_render_parts SET part_id=?,idx=?,name=?,bytes=?,payload_sha256=? '
                       'WHERE delivery_id=? AND part_id=?',
                       (f'body:{index + 1:04d}', index + 2, f'{group}#{index + 1}',
                        len(chunk.encode('utf-8')), text_sha256(chunk), delivery, f'part-{index}'))
        _seal_body_plan(db, delivery, chunks, retain=delivery == new)
    return old, new


def test_split_group_proof_survives_gc_without_body_or_replay(led):
    import notify_cards
    import notify_spool

    old, new = _split_repair(led.db)
    before = led.db.total_changes
    assert notify_scope.incomplete_parts_health(led.db, NOW, SLACK)['total'] == 0
    assert led.db.total_changes == before
    notify_cards.gc(led, SLACK, now=NOW + 1)
    raw = notify_scope.render_row(led.db, new)['spec_json']
    assert set(json.loads(raw)) == {'body_delivery_coverage'}
    assert 'SYNTHETIC_PRIVATE_BODY' not in raw and 'thread_body_parts' not in raw
    assert notify_scope.incomplete_parts_health(led.db, NOW + 2, SLACK)['total'] == 0
    notify_cards.gc(led, SLACK, now=NOW + 3)
    assert notify_scope.render_row(led.db, new)['spec_json'] == raw
    assert notify_spool.recover(led, SLACK, {})['republished'] == 0
    assert notify_scope.incomplete_parts_health(led.db, NOW + 4, SLACK)['total'] == 0
    assert {row[0] for row in led.db.execute(
        'SELECT state FROM notification_render_parts WHERE delivery_id=?', (old,))} == {'not_sent'}


@pytest.mark.parametrize('difference', ['scope', 'remote', 'duplicate_remote', 'hash', 'bytes',
                                       'missing_spec', 'partial', 'old_bytes', 'old_unknown',
                                       'source', 'header', 'changed'])
def test_split_proof_requires_complete_matching_group(stored, difference):
    old, new = _split_repair(stored, new_group={'source': 'm:101', 'header': 'm:100:header'}.get(difference, 'm:100'),
                             changed=difference == 'changed')
    if difference == 'scope':
        stored.execute("UPDATE notification_renders SET channel_id='SYNTHETIC_OTHER' WHERE delivery_id=?", (new,))
    elif difference == 'remote':
        stored.execute("UPDATE notification_render_parts SET remote_id='' WHERE delivery_id=? AND part_id='body:0001'", (new,))
    elif difference == 'duplicate_remote':
        stored.execute("UPDATE notification_render_parts SET remote_id='SYNTHETIC_SAME' WHERE delivery_id=?", (new,))
    elif difference == 'hash':
        stored.execute("UPDATE notification_render_parts SET payload_sha256=? WHERE delivery_id=? AND part_id='body:0001'", ('a' * 64, new))
    elif difference == 'bytes':
        stored.execute("UPDATE notification_render_parts SET bytes=bytes+1 WHERE delivery_id=? AND part_id='body:0001'", (new,))
    elif difference == 'missing_spec':
        stored.execute('UPDATE notification_renders SET spec_json=NULL WHERE delivery_id=?', (new,))
    elif difference == 'partial':
        stored.execute("UPDATE notification_render_parts SET state='unknown' WHERE delivery_id=? AND part_id='body:0001'", (new,))
    elif difference == 'old_bytes':
        stored.execute("UPDATE notification_render_parts SET bytes=bytes+1 WHERE delivery_id=? AND part_id='body:0001'", (old,))
    elif difference == 'old_unknown':
        stored.execute("UPDATE notification_render_parts SET state='unknown' WHERE delivery_id=?", (old,))
    report = notify_scope.incomplete_parts_health(stored, NOW, SLACK)
    assert report['by_state']['unknown' if difference == 'old_unknown' else 'not_sent'] >= 2


@pytest.mark.parametrize('difference', ['old_hash', 'new_remote', 'new_unknown', 'new_scope',
                                       'payload_hash', 'version', 'extra_content'])
def test_compact_split_proof_revalidates_saved_evidence(led, difference):
    import notify_cards

    old, new = _split_repair(led.db)
    notify_cards.gc(led, SLACK, now=NOW + 1)
    assert notify_scope.incomplete_parts_health(led.db, NOW + 2, SLACK)['total'] == 0
    if difference == 'old_hash':
        led.db.execute("UPDATE notification_render_parts SET payload_sha256=? WHERE delivery_id=? AND part_id='body:0001'", ('a' * 64, old))
    elif difference == 'new_remote':
        led.db.execute("UPDATE notification_render_parts SET remote_id='SYNTHETIC_CHANGED' WHERE delivery_id=? AND part_id='body:0001'", (new,))
    elif difference == 'new_unknown':
        led.db.execute("UPDATE notification_render_parts SET state='unknown' WHERE delivery_id=? AND part_id='body:0001'", (new,))
    elif difference == 'new_scope':
        led.db.execute("UPDATE notification_renders SET channel_id='SYNTHETIC_OTHER' WHERE delivery_id=?", (new,))
    else:
        witness = json.loads(notify_scope.render_row(led.db, new)['spec_json'])
        if difference == 'extra_content':
            witness['body_delivery_coverage']['source'] = 'SYNTHETIC_PRIVATE_CONTENT'
        else:
            witness['body_delivery_coverage'][difference] = 2 if difference == 'version' else 'a' * 64
        led.db.execute('UPDATE notification_renders SET spec_json=? WHERE delivery_id=?', (json.dumps(witness), new))
    assert notify_scope.incomplete_parts_health(led.db, NOW + 3, SLACK)['by_state']['not_sent'] == 2
    if difference == 'extra_content':
        notify_cards.gc(led, SLACK, now=NOW + 4)
        assert notify_scope.render_row(led.db, new)['spec_json'] is None


@pytest.mark.parametrize('difference', ['old_blob', 'new_blob', 'old_large_ordinal', 'new_large_ordinal'])
def test_malformed_body_names_do_not_stop_health_or_gc(led, difference):
    import notify_cards

    old, new = _split_repair(led.db)
    delivery = old if difference.startswith('old_') else new
    name = sqlite3.Binary(b'synthetic-nontext-name') if difference.endswith('_blob') else 'm:100#' + '9' * 5000
    led.db.execute("UPDATE notification_render_parts SET name=? WHERE delivery_id=? AND part_id='body:0001'",
                   (name, delivery))
    assert notify_scope.incomplete_parts_health(led.db, NOW, SLACK)['by_state']['not_sent'] == 2
    notify_cards.gc(led, SLACK, now=NOW + 1)
    assert notify_scope.incomplete_parts_health(led.db, NOW + 2, SLACK)['by_state']['not_sent'] == 2


def test_compact_split_proof_cannot_move_to_another_matching_scope(led):
    import notify_cards
    from mcs_util import canonical, payload_hash

    old, new = _split_repair(led.db)
    render = notify_scope.render_row(led.db, new)
    spec = json.loads(render['spec_json'])
    spec['render_rev'] = 4
    led.db.execute('UPDATE notification_renders SET render_rev=4,spec_json=?,payload_hash=? WHERE delivery_id=?',
                   (canonical(spec).decode(), payload_hash(spec), new))
    _, other = _render(led.db, [('body_part', 'not_sent', 'msg_too_long')] * 2,
                       card=render['card_id'], revision=3)
    led.db.execute("UPDATE notification_renders SET channel_id='SYNTHETIC_OTHER' WHERE delivery_id=?", (other,))
    original = ''.join(spec['parts']['thread_body_parts']).encode('utf-8')
    offset, other_chunks = 0, []
    for index, row in enumerate(led.db.execute(
            'SELECT * FROM notification_render_parts WHERE delivery_id=? ORDER BY idx', (old,)).fetchall()):
        led.db.execute('UPDATE notification_render_parts SET part_id=?,idx=?,name=?,bytes=?,payload_sha256=? '
                       'WHERE delivery_id=? AND part_id=?',
                       (row['part_id'], row['idx'], row['name'], row['bytes'], row['payload_sha256'],
                        other, f'part-{index}'))
        other_chunks.append(original[offset:offset + row['bytes']].decode('utf-8'))
        offset += row['bytes']
    _seal_body_plan(led.db, other, other_chunks, retain=False)
    notify_cards.gc(led, SLACK, now=NOW + 1)
    assert 'body_delivery_coverage' in json.loads(notify_scope.render_row(led.db, new)['spec_json'])
    assert notify_scope.incomplete_parts_health(led.db, NOW + 2, SLACK)['by_state']['not_sent'] == 2
    led.db.execute("UPDATE notification_renders SET channel_id='SYNTHETIC_OTHER' WHERE delivery_id=?", (new,))
    assert notify_scope.incomplete_parts_health(led.db, NOW + 3, SLACK)['by_state']['not_sent'] == 4


@pytest.mark.parametrize('side', ['old', 'new'])
@pytest.mark.parametrize('field,value', [
    ('correlation', sqlite3.Binary(b'synthetic-correlation')),
    ('route_epoch', sqlite3.Binary(b'synthetic-epoch')),
    ('render_rev', sqlite3.Binary(b'synthetic-revision')),
    ('render_rev', 'synthetic-nonnumeric-revision'),
])
def test_invalid_render_metadata_remains_unproven_without_stopping_health_or_gc(led, side, field, value):
    import notify_cards

    old, new = _split_repair(led.db)
    delivery = old if side == 'old' else new
    # Column names are the fixed parameter cases above, never external input.
    led.db.execute(f'UPDATE notification_renders SET {field}=? WHERE delivery_id=?', (value, delivery))
    assert notify_scope.incomplete_parts_health(led.db, NOW, SLACK)['by_state']['not_sent'] == 2
    notify_cards.gc(led, SLACK, now=NOW + 1)
    assert notify_scope.incomplete_parts_health(led.db, NOW + 2, SLACK)['by_state']['not_sent'] == 2


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
    assert set(report) == {'total', 'cards', 'by_state', 'by_plan',
                           'oldest_age_s', 'observed_at', 'active_total', 'active_cards',
                           'active_by_state', 'active_by_plan', 'active_oldest_age_s',
                           'acknowledged_unresolved_total', 'unknown_parts_total'}
    assert report['active_total'] == report['total'] and report['active_cards'] == report['cards']
    assert report['active_by_state'] == report['by_state'] and report['active_by_plan'] == report['by_plan']
    assert report['active_oldest_age_s'] == report['oldest_age_s']
    assert report['acknowledged_unresolved_total'] == 0 and report['unknown_parts_total'] == 1
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


def test_failed_part_replans_on_the_next_render_without_duplicating(led, tmp_path):
    """An unsent surface and its files retry together; proven posts retain their ids."""
    import notify_cards
    from notify_testkit import (CFG, NOW as DELIVERY_NOW, _attach, _begin, _dispatch, _intent,
                                _latest_render, _part_receipt, _receipt, _seed_thread,
                                _surface_file_echo, _wire_parts)

    _seed_thread(led, mids=(100,))
    path = tmp_path / 'synthetic.bin'
    blob = b'synthetic attachment'
    path.write_bytes(blob)
    _attach(led, 100, local_path=str(path),
            sha256=hashlib.sha256(blob).hexdigest(), nbytes=len(blob))
    assert _dispatch(led, _intent(led, payload={'message_ids': [100]}))['dispatched']
    first = _latest_render(led)
    spec = json.loads(first['spec_json'])
    group, = [p for p in spec['parts']['manifest'] if p['kind'] == 'file_group']
    failed_part = group['surface']
    assert failed_part != 'card'
    assert _begin(led, first)['granted']
    assert _receipt(led, first, f'{1:016x}', message_id='synthetic-card')['applied']
    for n, part in enumerate(_wire_parts(spec)[1:], 60):
        if part['part_id'] == failed_part:
            assert _part_receipt(led, first, failed_part, result='not_sent',
                                 remote_id=None, error_code='http_400', n=n)['applied']
        else:
            assert _part_receipt(led, first, part['part_id'], remote_id='r-' + part['part_id'],
                                 n=n, files=_surface_file_echo(spec, part['part_id']))['applied']
    owed = {failed_part, group['part_id'], *group['members']}
    states = {row['part_id']: row['state'] for row in led.db.execute(
        'SELECT part_id,state FROM notification_render_parts WHERE delivery_id=?',
        (first['delivery_id'],))}
    assert {pid for pid, state in states.items() if state == 'not_sent'} == owed
    with led.db:
        assert notify_cards.issue_render(led.db, first['card_id'], CFG, DELIVERY_NOW + 1, [], force=True)
    later = _latest_render(led)
    assert later['delivery_id'] != first['delivery_id'] and later['state'] == 'queued'
    later_spec = json.loads(later['spec_json'])
    manifest = {p['part_id']: p for p in later_spec['parts']['manifest']}
    assert all(not manifest[pid].get('prior_remote_id') for pid in owed)
    assert any(p['kind'] == 'body_part' and p.get('prior_remote_id')
               for p in manifest.values())
    report = notify_scope.incomplete_parts_health(led.db, DELIVERY_NOW + 1, CFG)
    assert report['total'] == len(owed)
    assert report['by_plan'] == {'current': len(owed), 'superseded': 0}
    assert _begin(led, later, n=2)['granted']
    assert _receipt(led, later, f'{2:016x}', message_id='synthetic-card-2', n=10)['applied']
    for n, part in enumerate(_wire_parts(later_spec)[1:], 200):
        assert _part_receipt(led, later, part['part_id'], remote_id='r2-' + part['part_id'],
                             n=n, files=_surface_file_echo(later_spec, part['part_id']))['applied']
    report = notify_scope.incomplete_parts_health(led.db, DELIVERY_NOW + 2, CFG)
    assert report['total'] == 0
    assert report['by_plan'] == {'current': 0, 'superseded': 0}


def test_by_plan_splits_live_debt_from_superseded_plan_history(stored):
    card, _ = _render(stored, [('body_part', 'not_sent', 'synthetic_failure')])
    _render(stored, [('body_part', 'not_sent', 'synthetic_failure')], card=card, revision=2)
    report = notify_scope.incomplete_parts_health(stored, NOW, SLACK)
    assert report['total'] == 2 and report['cards'] == 1
    assert report['by_plan'] == {'current': 1, 'superseded': 1}


def test_by_plan_proven_parts_neither_current_nor_superseded(stored):
    card, _ = _render(stored, [('body_part', 'not_sent', 'synthetic_failure')])
    _render(stored, [('body_part', 'delivered', None)], card=card, revision=2)
    report = notify_scope.incomplete_parts_health(stored, NOW, SLACK)
    assert report['total'] == 0
    assert report['by_plan'] == {'current': 0, 'superseded': 0}


@pytest.mark.parametrize('new_state', ['queued', 'sending', 'held', 'not_sent', 'cancelled', 'unknown'])
def test_unconfirmed_later_render_keeps_the_delivered_plan_current(stored, new_state):
    card, _ = _render(stored, [('body_part', 'unknown', None)])
    _render(stored, [('body_part', 'pending', None)], card=card, revision=2,
            render_state=new_state)
    report = notify_scope.incomplete_parts_health(stored, NOW, SLACK)
    assert report['by_plan'] == {'current': 1, 'superseded': 0}


def test_by_plan_marks_only_the_same_card_newer_delivered_render(stored):
    card, _ = _render(stored, [('body_part', 'unknown', None)])
    _render(stored, [('body_part', 'unknown', None)])   # a different card's plan proves nothing
    _render(stored, [('body_part', 'delivered', None)], card=card, revision=2,
            render_state='queued')
    report = notify_scope.incomplete_parts_health(stored, NOW, SLACK)
    assert report['total'] == 2 and report['cards'] == 2
    assert report['by_plan'] == {'current': 2, 'superseded': 0}
    _render(stored, [('body_part', 'delivered', None)], card=card, revision=3)
    report = notify_scope.incomplete_parts_health(stored, NOW, SLACK)
    assert report['total'] == 1
    assert report['by_plan'] == {'current': 1, 'superseded': 0}


def test_by_plan_is_card_level_even_when_the_newer_scope_differs(stored):
    card, _ = _render(stored, [('body_part', 'unknown', None)])
    _, moved = _render(stored, [('body_part', 'delivered', None)], card=card, revision=2)
    stored.execute("UPDATE notification_renders SET channel_id='synthetic-channel-2' "
                   "WHERE delivery_id=?", (moved,))
    report = notify_scope.incomplete_parts_health(stored, NOW, SLACK)
    assert report['total'] == 1
    assert report['by_plan'] == {'current': 0, 'superseded': 1}


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


def _discord_health_groups(db, surface_kind):
    states = [(surface_kind, 'delivered', None), ('attachment_part', 'delivered', None),
              ('attachment_part', 'delivered', None), ('file_group', 'unknown', None)]
    card, old = _render(db, states, transport='discord')
    _, new = _render(db, states[:-1] + [('file_group', 'delivered', None)],
                     transport='discord', card=card, revision=2)
    surface = 'card' if surface_kind == 'card' else 'body:0001'
    for delivery in (old, new):
        db.execute("UPDATE notification_render_parts SET part_id=?,name=? WHERE delivery_id=? AND idx=0",
                   (surface, 'm:100#1' if surface_kind == 'body_part' else None, delivery))
        db.execute("UPDATE notification_render_parts SET part_id=?,name=?,payload_sha256=? "
                   "WHERE delivery_id=? AND idx=3",
                   (f'file_group:{surface}', surface, hashlib.sha256(delivery.encode()).hexdigest(), delivery))
        db.execute("UPDATE notification_render_parts SET bytes=25,attempt_id=?,remote_id=? "
                   "WHERE delivery_id=?", (f'attempt-{delivery}', f'remote-{delivery}', delivery))
        db.execute("UPDATE notification_render_parts SET remote_file_id='file-' || attachment_id "
                   "WHERE delivery_id=? AND kind='attachment_part'", (delivery,))
    return old, new, f'file_group:{surface}'


@pytest.mark.parametrize('surface_kind', ['card', 'body_part'])
def test_discord_group_coverage_survives_spec_gc_and_is_read_only(stored, surface_kind):
    old, new, group = _discord_health_groups(stored, surface_kind)
    # Both immutable specs have already been collected; only durable part proofs remain.
    assert all(notify_scope.render_row(stored, d)['spec_json'] is None for d in (old, new))
    before = stored.total_changes
    stored.execute('PRAGMA query_only=ON')
    try:
        assert notify_scope.incomplete_parts_health(stored, NOW)['total'] == 0
        assert stored.total_changes == before
        assert stored.execute('SELECT state FROM notification_render_parts WHERE delivery_id=? AND part_id=?',
                              (old, group)).fetchone()[0] == 'unknown'
    finally:
        stored.execute('PRAGMA query_only=OFF')


@pytest.mark.parametrize('difference', ['scope', 'missing', 'hash', 'name', 'bytes', 'file_id',
                                       'duplicate_file_id', 'group', 'surface', 'attempt', 'remote',
                                       'source', 'old_hash'])
def test_discord_group_coverage_requires_all_bound_member_proofs(stored, difference):
    old, new, group = _discord_health_groups(stored, 'body_part')
    if difference == 'scope':
        stored.execute("UPDATE notification_renders SET channel_id='other' WHERE delivery_id=?", (new,))
    elif difference == 'missing':
        stored.execute('DELETE FROM notification_render_parts WHERE delivery_id=? AND idx=2', (new,))
    elif difference == 'group':
        stored.execute("UPDATE notification_render_parts SET state='unknown' WHERE delivery_id=? AND idx=3", (new,))
    elif difference == 'surface':
        stored.execute("UPDATE notification_render_parts SET state='unknown' WHERE delivery_id=? AND idx=0", (new,))
    elif difference == 'source':
        stored.execute("UPDATE notification_render_parts SET name='m:101#1' WHERE delivery_id=? AND idx=0", (new,))
    else:
        column, value = {
            'hash': ('payload_sha256', 'a' * 64), 'name': ('name', 'different.pdf'),
            'bytes': ('bytes', 26), 'file_id': ('remote_file_id', None),
            'duplicate_file_id': ('remote_file_id', 'file-3'),
            'attempt': ('attempt_id', 'different-attempt'), 'remote': ('remote_id', 'different-message'),
            'old_hash': ('payload_sha256', None),
        }[difference]
        stored.execute(f'UPDATE notification_render_parts SET {column}=? WHERE delivery_id=? AND idx=1',
                       (value, old if difference == 'old_hash' else new))
    before = stored.total_changes
    remaining = {(r['delivery_id'], r['part_id']) for r in notify_scope._incomplete_part_rows(stored)}
    assert (old, group) in remaining
    assert stored.total_changes == before
