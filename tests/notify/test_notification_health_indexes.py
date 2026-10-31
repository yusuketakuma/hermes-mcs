"""Notification health indexes preserve results and bound history scans."""
from types import SimpleNamespace

import notify_recovery
import notify_scope
import pytest
from common_testkit import make_ledger


INDEXES = ('idx_nparts_state_delivery', 'idx_nattempts_unsettled_created')
TABLES = ('notification_cards', 'notification_renders', 'notification_render_parts',
          'notification_delivery_attempts')


def _seed(db):
    cards, renders, parts, attempts = [], [], [], []
    for card in range(1, 161):
        cards.append((card, f'synthetic-card-{card}', 'thread', 'synthetic-anchor',
                      'delivered', float(card), float(card)))
        for rev in range(1, 9):
            seq = (card - 1) * 8 + rev
            delivery = f'synthetic-delivery-{seq}'
            renders.append((delivery, card, 'update', rev, 1, f'hash-{seq}',
                            f'correlation-{seq}', 'delivered', float(seq), float(seq)))
            state = 'unknown' if seq % 43 == 0 else 'granted' if seq % 47 == 0 else 'delivered'
            attempts.append((f'attempt-{seq}', delivery, f'command-{seq}', state, float(seq)))
            for part in range(10):
                part_state = ('unknown' if seq % 41 == 0 and part == 1 else
                              'not_sent' if seq % 37 == 0 and part == 2 else 'delivered')
                parts.append((delivery, f'part-{part}', 'body_part', part, part_state,
                              f'synthetic-part-{part}', float(seq * 10 + part)))
    with db:
        db.executemany(
            'INSERT INTO notification_cards(card_id,card_key,kind,anchor_key,delivery_state,'
            'created_at,updated_at) VALUES(?,?,?,?,?,?,?)', cards)
        db.executemany(
            'INSERT INTO notification_renders(delivery_id,card_id,op,render_rev,route_epoch,'
            'payload_hash,correlation,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)', renders)
        db.executemany(
            'INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,state,name,updated_at) '
            'VALUES(?,?,?,?,?,?,?)', parts)
        db.executemany(
            'INSERT INTO notification_delivery_attempts(attempt_id,delivery_id,begin_command_id,'
            'state,created_at) VALUES(?,?,?,?,?)', attempts)


def _rows(db):
    return {name: sorted((tuple(row) for row in db.execute(f'SELECT * FROM {name}')), key=repr)
            for name in TABLES}


def _observe(db):
    return (notify_scope.layout_migration_counts(db),
            notify_scope.incomplete_parts_health(db, 100_000),
            notify_recovery.part_hold_report(db), notify_scope.health_cards(db))


def _operations(db, query):
    steps = 0

    def progress():
        nonlocal steps
        steps += 1
        return 0

    db.set_progress_handler(progress, 100)
    try:
        result = query(db)
    finally:
        db.set_progress_handler(None, 0)
    return result, steps


def test_reopen_adds_indexes_without_changing_health_or_history(tmp_path, monkeypatch):
    monkeypatch.setattr(notify_scope, 'time', SimpleNamespace(time=lambda: 100_000.0))
    store = make_ledger(tmp_path)
    try:
        _seed(store.db)
        for name in INDEXES:
            store.db.execute(f'DROP INDEX {name}')
        store.db.commit()
        version = store.db.execute('PRAGMA user_version').fetchone()[0]
        original_rows = _rows(store.db)
        original, old_ops = _operations(store.db, _observe)
        original_layout, old_layout_ops = _operations(store.db, notify_scope.layout_migration_counts)
        store.close()

        store = make_ledger(tmp_path)
        assert store.db.execute('PRAGMA user_version').fetchone()[0] == version
        assert _rows(store.db) == original_rows
        indexes = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert set(INDEXES) <= indexes
        assert [row[2] for row in store.db.execute(f'PRAGMA index_info({INDEXES[0]})')] == [
            'state', 'delivery_id']
        assert [row[2] for row in store.db.execute(f'PRAGMA index_info({INDEXES[1]})')] == [
            'created_at', 'delivery_id']
        updated, new_ops = _operations(store.db, _observe)
        updated_layout, new_layout_ops = _operations(store.db, notify_scope.layout_migration_counts)
        assert updated == original
        assert updated_layout == original_layout
        assert new_layout_ops < old_layout_ops
        assert new_ops < old_ops
        store.close()

        store = make_ledger(tmp_path)
        assert _rows(store.db) == original_rows
        assert _observe(store.db) == original
        assert store.db.execute('PRAGMA user_version').fetchone()[0] == version
    finally:
        store.close()


def _seed_scope_history(db, mixed):
    cards, renders, parts = [], [], []
    for card in range(1, 33):
        cards.append((card, f'synthetic-card-{card}', 'thread', 'synthetic-anchor',
                      'slack', 'delivered', 1.0, 1.0))
        for rev in range(1, 49):
            delivery = f'synthetic-delivery-{card}-{rev}'
            profile = f'profile-{rev % 8}' if mixed and rev > 3 else 'profile-0'
            renders.append((delivery, card, rev, profile, 'synthetic-app', 'synthetic-team',
                            'synthetic-channel', delivery, float(rev), float(rev)))
            for part in range(3):
                state = 'unknown' if rev <= 3 and part == 0 else 'delivered'
                digest = ('a' if state == 'unknown' or (card == 1 and rev == 48 and part == 0)
                          else 'b') * 64
                parts.append((delivery, f'part-{part}', 'body_part', part, state,
                              f'body-{part}', digest,
                              'synthetic-remote' if state == 'delivered' else None, float(rev)))
    with db:
        db.executemany(
            'INSERT INTO notification_cards(card_id,card_key,kind,anchor_key,transport,'
            'delivery_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)', cards)
        db.executemany(
            'INSERT INTO notification_renders(delivery_id,card_id,op,render_rev,route_epoch,'
            'transport,profile,application_id,team_id,channel_id,payload_hash,correlation,'
            "state,created_at,updated_at) VALUES(?,?,'update',?,1,'slack',?,?,?,?,'h',?,"
            "'delivered',?,?)", renders)
        db.executemany(
            'INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,state,name,'
            'payload_sha256,remote_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?)', parts)


@pytest.mark.parametrize('mixed', [False, True], ids=['same-scope', 'mixed-scopes'])
def test_delivered_scope_index_preserves_results_and_reduces_vm_work(tmp_path, mixed):
    index = 'idx_nrenders_delivered_scope_rev'
    store = make_ledger(tmp_path)
    try:
        _seed_scope_history(store.db, mixed)
        store.db.execute(f'DROP INDEX {index}')
        store.db.commit()
        version = store.db.execute('PRAGMA user_version').fetchone()[0]
        original_rows = _rows(store.db)
        original, old_ops = _operations(
            store.db, lambda db: notify_scope.incomplete_parts_health(db, 100_000))
        assert original['total'] == 93
        assert original['by_state']['unknown'] == 93
        store.close()

        store = make_ledger(tmp_path)
        assert store.db.execute('PRAGMA user_version').fetchone()[0] == version
        assert _rows(store.db) == original_rows
        assert [row[2] for row in store.db.execute(f'PRAGMA index_info({index})')] == [
            'card_id', 'transport', 'profile', 'application_id', 'guild_id', 'team_id',
            'channel_id', 'render_rev']
        sql = store.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index,)).fetchone()[0]
        assert "WHERE state='delivered'" in sql
        updated, new_ops = _operations(
            store.db, lambda db: notify_scope.incomplete_parts_health(db, 100_000))
        assert updated == original
        assert new_ops < old_ops
        store.close()

        store = make_ledger(tmp_path)
        assert _rows(store.db) == original_rows
        assert notify_scope.incomplete_parts_health(store.db, 100_000) == original
        assert store.db.execute('PRAGMA user_version').fetchone()[0] == version
        assert store.db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?", (index,)).fetchone()[0] == 1
    finally:
        store.close()
