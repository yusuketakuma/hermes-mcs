"""Sealed rejected bodies survive GC until exact delivery evidence exists."""
import json
import sqlite3
from pathlib import Path

import pytest

import notify_cards
import notify_scope
import notify_spool
from notify_testkit import led as led
from test_incomplete_part_health import NOW, SLACK, _seal_body_plan, _split_repair


def _sealed_rejection(led, difference=None):
    old, new = _split_repair(led.db, changed=difference == 'changed')
    text = 'SYNTHETIC_PRIVATE_BODY\n' + 'あ' * 2100 + '\n' + 'é' * 2300
    _seal_body_plan(led.db, old, [text[:2200], text[2200:]], retain=True)
    if difference == 'pending':
        led.db.execute("UPDATE notification_render_parts SET state='pending' WHERE delivery_id=?", (new,))
    elif difference == 'scope':
        led.db.execute("UPDATE notification_renders SET channel_id='synthetic-other' WHERE delivery_id=?", (new,))
    elif difference == 'hash':
        led.db.execute('UPDATE notification_render_parts SET payload_sha256=? WHERE delivery_id=?', ('a' * 64, new))
    root = notify_spool.data_root(led)
    notify_spool.ensure_dirs(root)
    dirs = notify_spool.notify_dirs(root)
    # The spool copy and DB copy are independently necessary recovery evidence.
    path = Path(dirs['slack_render']) / (old + '.json')
    raw = notify_scope.render_row(led.db, old)['spec_json']
    path.write_text(raw)
    led.db.execute('UPDATE notification_renders SET spec_published=1 WHERE delivery_id=?', (old,))
    return old, new, raw, path


@pytest.mark.parametrize('difference', ['pending', 'scope', 'hash', 'changed'])
def test_unresolved_length_rejection_keeps_both_sealed_copies(led, difference):
    old, _, raw, path = _sealed_rejection(led, difference)
    notify_cards.gc(led, SLACK, now=NOW + 1)
    assert notify_scope.render_row(led.db, old)['spec_json'] == raw
    assert path.read_text() == raw
    assert notify_scope.render_row(led.db, old)['spec_published'] == 1
    assert notify_scope.incomplete_parts_health(led.db, NOW + 2, SLACK)['total'] >= 2


def test_complete_matching_repair_releases_body_and_keeps_hash_witness(led):
    old, new, _, path = _sealed_rejection(led)
    notify_cards.gc(led, SLACK, now=NOW + 1)
    assert notify_scope.render_row(led.db, old)['spec_json'] is None
    assert not path.exists()
    assert set(json.loads(notify_scope.render_row(led.db, new)['spec_json'])) == {'body_delivery_coverage'}
    assert notify_scope.incomplete_parts_health(led.db, NOW + 2, SLACK)['total'] == 0


def test_unrelated_terminal_failure_keeps_existing_gc_contract(led):
    old, _, _, path = _sealed_rejection(led, 'pending')
    led.db.execute("UPDATE notification_render_parts SET error_code='synthetic_other_failure' WHERE delivery_id=?", (old,))
    notify_cards.gc(led, SLACK, now=NOW + 1)
    assert notify_scope.render_row(led.db, old)['spec_json'] is None
    assert not path.exists()


def test_retained_pages_are_bounded_and_resume_after_restart(led, monkeypatch):
    retained = [_sealed_rejection(led, 'pending') for _ in range(3)]
    old, _, _, path = _sealed_rejection(led, 'pending')
    led.db.execute("UPDATE notification_render_parts SET error_code='synthetic_other_failure' WHERE delivery_id=?", (old,))
    led.db.execute('UPDATE notification_renders SET updated_at=? WHERE delivery_id=?', (NOW + 10, old))
    examined = []
    check = notify_scope.unresolved_length_deliveries

    def bounded(db, delivery_ids, **kwargs):
        examined.append(list(delivery_ids))
        return check(db, delivery_ids, **kwargs)

    monkeypatch.setattr(notify_scope, 'unresolved_length_deliveries', bounded)
    for tick in range(3):
        result = notify_cards.gc(led, SLACK, now=NOW + 11 + tick, limit=1)
        assert result['spec_json_cleared'] == result['spec_files'] == 0
    database = led.db.execute('PRAGMA database_list').fetchone()['file']
    with sqlite3.connect(database) as restarted:
        restarted.row_factory = sqlite3.Row
        result = notify_cards.gc(restarted, SLACK, now=NOW + 14, limit=1)
    assert len(examined) == 8 and all(len(page) <= 1 for page in examined)
    assert result['spec_json_cleared'] == result['spec_files'] == 1
    assert notify_scope.render_row(led.db, old)['spec_json'] is None and not path.exists()
    for delivery, _, raw, retained_path in retained:
        assert notify_scope.render_row(led.db, delivery)['spec_json'] == raw
        assert retained_path.read_text() == raw


def test_retained_rejection_is_released_after_verified_completion(led):
    old, new, _, path = _sealed_rejection(led, 'pending')
    notify_cards.gc(led, SLACK, now=NOW + 1)
    assert path.exists()
    led.db.execute("UPDATE notification_render_parts SET state='delivered' WHERE delivery_id=?", (new,))
    notify_cards.gc(led, SLACK, now=NOW + 2)
    assert notify_scope.render_row(led.db, old)['spec_json'] is None
    assert not path.exists()
    assert notify_scope.incomplete_parts_health(led.db, NOW + 3, SLACK)['total'] == 0


def _cursor_path(led):
    return Path(notify_spool.notify_dirs(notify_spool.data_root(led))['flags']) / 'notify_gc_cursor.json'


@pytest.mark.parametrize('malformed', ['syntax', 'duplicate', 'version', 'timestamp', 'identity'])
def test_malformed_cursor_restarts_bounded_scan_without_releasing_facts(led, malformed):
    old, _, raw, path = _sealed_rejection(led, 'pending')
    notify_cards.gc(led, SLACK, now=NOW + 1, limit=1)
    cursor_path = _cursor_path(led)
    cursor = json.loads(cursor_path.read_text())
    if malformed == 'version':
        cursor['version'] = True
    elif malformed == 'timestamp':
        cursor['positions']['spec_json'][0] = True
    elif malformed == 'identity':
        cursor['database'][1] += 1
    encoded = json.dumps(cursor)
    if malformed == 'syntax':
        encoded = '{broken'
    elif malformed == 'duplicate':
        encoded = encoded.replace('{', '{"version":1,', 1)
    cursor_path.write_text(encoded)
    result = notify_cards.gc(led, SLACK, now=NOW + 2, limit=1)
    assert result['spec_json_cleared'] == result['spec_files'] == 0
    assert notify_scope.render_row(led.db, old)['spec_json'] == raw and path.read_text() == raw
    saved = json.loads(cursor_path.read_text())
    assert saved['positions']['spec_json'] == [NOW, old]
    assert cursor_path.stat().st_mode & 0o777 == 0o600


def test_cursor_wrap_revisits_a_retained_page(led):
    first, _, _, _ = _sealed_rejection(led, 'pending')
    _sealed_rejection(led, 'pending')
    for tick in range(3):
        notify_cards.gc(led, SLACK, now=NOW + tick + 1, limit=1)
    assert json.loads(_cursor_path(led).read_text())['positions']['spec_json'] is None
    led.db.execute("UPDATE notification_render_parts SET error_code='synthetic_other_failure' WHERE delivery_id=?", (first,))
    result = notify_cards.gc(led, SLACK, now=NOW + 4, limit=1)
    assert result['spec_json_cleared'] == 1
    assert notify_scope.render_row(led.db, first)['spec_json'] is None


def test_failed_page_does_not_publish_cursor_or_commit_clearing(led, monkeypatch):
    old, _, raw, path = _sealed_rejection(led, 'pending')
    led.db.execute("UPDATE notification_render_parts SET error_code='synthetic_other_failure' WHERE delivery_id=?", (old,))
    led.db.commit()
    check = notify_scope.unresolved_length_deliveries
    calls = 0

    def fail_second_pass(db, delivery_ids, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('synthetic-page-failure')
        return check(db, delivery_ids, **kwargs)

    monkeypatch.setattr(notify_scope, 'unresolved_length_deliveries', fail_second_pass)
    with pytest.raises(RuntimeError, match='synthetic-page-failure'):
        notify_cards.gc(led, SLACK, now=NOW + 1, limit=1)
    assert not _cursor_path(led).exists()
    assert notify_scope.render_row(led.db, old)['spec_json'] == raw and path.read_text() == raw
