"""Synthetic layout counts preserve history and avoid repeated correlated scans."""
from contextlib import closing
import sqlite3

import pytest

import notify_scope

BASELINE = """
    SELECT c.transport,c.layout,c.delivery_state,COUNT(*) AS cards,
           SUM(EXISTS(SELECT 1 FROM notification_render_parts p
               JOIN notification_renders r USING(delivery_id)
               WHERE r.card_id=c.card_id AND p.state='unknown')) AS unknown_parts
    FROM notification_cards c GROUP BY c.transport,c.layout,c.delivery_state
    ORDER BY c.transport,c.layout,c.delivery_state
"""


def database(*, indexed=False):
    # Only columns read by the query. PKs preserve actual join uniqueness.
    # The unassisted variant exercises the correlated plan's scan risk without
    # changing production indexes or depending on a particular query planner.
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE notification_cards(card_id INTEGER PRIMARY KEY,
            transport TEXT, layout INTEGER, delivery_state TEXT);
        CREATE TABLE notification_renders(delivery_id TEXT PRIMARY KEY,
            card_id INTEGER, render_rev INTEGER);
        CREATE TABLE notification_render_parts(delivery_id TEXT, part_id TEXT,
            state TEXT, PRIMARY KEY(delivery_id,part_id));
    """)
    if indexed:
        db.executescript("""
            CREATE UNIQUE INDEX renders_revision ON notification_renders(card_id,render_rev);
            CREATE INDEX renders_card ON notification_renders(card_id);
        """)
    return db


def old_counts(db):
    return [dict(row) for row in db.execute(BASELINE)]


def history(db):
    db.executemany('INSERT INTO notification_cards VALUES(?,?,?,?)', [
        (1, 'discord', 1, 'delivered'), (2, 'discord', 1, 'delivered'),
        (3, 'slack', 2, 'delivery_unknown'), (4, 'lineworks', 3, 'pending'),
        (5, 'slack', 3, 'update_failed'), (6, 'lineworks', 1, 'revoked'),
        (7, 'discord', 2, 'message_deleted')])
    db.executemany('INSERT INTO notification_renders VALUES(?,?,?)', [
        ('old1', 1, 1), ('new1', 1, 2), ('old3', 3, 1), ('new3', 3, 2),
        ('new4', 4, 1), ('new5', 5, 1), ('new6', 6, 1), ('dangling', 999, 1),
        ('unassigned', None, 1)])
    db.executemany('INSERT INTO notification_render_parts VALUES(?,?,?)', [
        ('old1', 'a', 'unknown'), ('old1', 'b', 'unknown'), ('new1', 'a', 'delivered'),
        ('new1', 'b', 'unknown'), ('old3', 'a', 'unknown'), ('new3', 'a', 'delivered'),
        ('new4', 'a', 'pending'), ('new5', 'a', 'held'), ('new6', 'a', 'unknown'),
        ('dangling', 'a', 'unknown'), ('unassigned', 'a', 'unknown'), ('no-render', 'a', 'unknown')])
    db.commit()


@pytest.mark.parametrize('indexed', [False, True])
def test_multiple_unknown_parts_and_renders_count_one_card_and_retain_all_groups(indexed):
    with closing(database(indexed=indexed)) as db:
        history(db)
        before = db.total_changes
        expected = [
            {'transport': 'discord', 'layout': 1, 'delivery_state': 'delivered', 'cards': 2, 'unknown_parts': 1},
            {'transport': 'discord', 'layout': 2, 'delivery_state': 'message_deleted', 'cards': 1, 'unknown_parts': 0},
            {'transport': 'lineworks', 'layout': 1, 'delivery_state': 'revoked', 'cards': 1, 'unknown_parts': 1},
            {'transport': 'lineworks', 'layout': 3, 'delivery_state': 'pending', 'cards': 1, 'unknown_parts': 0},
            {'transport': 'slack', 'layout': 2, 'delivery_state': 'delivery_unknown', 'cards': 1, 'unknown_parts': 1},
            {'transport': 'slack', 'layout': 3, 'delivery_state': 'update_failed', 'cards': 1, 'unknown_parts': 0},
        ]
        assert notify_scope.layout_migration_counts(db) == old_counts(db) == expected
        assert db.total_changes == before
        assert db.execute('SELECT COUNT(*) FROM notification_render_parts').fetchone()[0] == 12
        assert db.execute('SELECT COUNT(*) FROM notification_renders').fetchone()[0] == 9


@pytest.mark.parametrize('indexed', [False, True])
def test_empty_and_no_unknown_parts_preserve_zero_counts(indexed):
    with closing(database(indexed=indexed)) as db:
        assert notify_scope.layout_migration_counts(db) == old_counts(db) == []
        history(db)
        db.execute("UPDATE notification_render_parts SET state='delivered'")
        before = db.total_changes
        result = notify_scope.layout_migration_counts(db)
        assert result == old_counts(db) and len(result) == 6
        assert all(row['unknown_parts'] == 0 for row in result)
        assert sum(row['cards'] for row in result) == 7
        assert db.total_changes == before


def scaled_history(db, cards):
    # Largest case: 192 cards, 384 renders, 2304 fully synthetic parts.
    transports = ('discord', 'slack', 'lineworks')
    db.executemany('INSERT INTO notification_cards VALUES(?,?,?,?)',
                   [(i, transports[i % 3], 1 + i % 3, 'delivered') for i in range(1, cards + 1)])
    renders, parts = [], []
    for card in range(1, cards + 1):
        for revision in (1, 2):
            delivery = f'synthetic-{card}-{revision}'
            renders.append((delivery, card, revision))
            for part in range(6):
                # No unknowns on most cards forces the baseline EXISTS to prove
                # absence; multiple unknowns on a few cards still count once.
                state = 'unknown' if card % 8 == 0 and revision == 1 else 'delivered'
                parts.append((delivery, str(part), state))
    db.executemany('INSERT INTO notification_renders VALUES(?,?,?)', renders)
    db.executemany('INSERT INTO notification_render_parts VALUES(?,?,?)', parts)
    db.commit()


def vm_steps(db, query):
    steps = 0
    def progress():
        nonlocal steps
        steps += 1
        return 0
    db.set_progress_handler(progress, 1)
    try:
        result = query(db)
    finally:
        db.set_progress_handler(None, 0)
    return result, steps


def test_vm_operation_growth_avoids_card_times_history_scan():
    operations = []
    for size in (48, 192):
        with closing(database()) as db:
            scaled_history(db, size)
            before = db.total_changes
            expected, baseline = vm_steps(db, old_counts)
            result, candidate = vm_steps(db, notify_scope.layout_migration_counts)
            assert result == expected
            assert sum(row['cards'] for row in result) == size
            assert sum(row['unknown_parts'] for row in result) == size // 8
            assert db.total_changes == before
            operations.append((baseline, candidate))
    small, large = operations
    # Four times the rows may add fixed/planner overhead; avoid timing assertions
    # or exact opcode totals that depend on the installed SQLite release.
    assert large[1] <= small[1] * 6
    assert large[0] >= small[0] * 8
    assert large[1] * 2 < large[0]
