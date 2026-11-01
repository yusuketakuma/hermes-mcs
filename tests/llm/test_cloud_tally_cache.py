"""Synthetic unchanged-read reuse never replaces transactional budget checks."""
from contextlib import closing, nullcontext
import json
import re
import sqlite3
from types import SimpleNamespace

import pytest

import cloud_policy as c
from anthropic_client import AnthropicError, MODEL
from ledger import Ledger
from test_cloud_policy import _cfg, _late_results, _reserve, now

__all__ = ['now']


def _read(ledger, period, now):
    return c._tally(ledger, now) if period == 'month' else c._window_tally(ledger, now - 10)


@pytest.mark.parametrize('period', ['month', 'window'])
def test_unchanged_precheck_avoids_accounting_scans(tmp_path, now, period):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        _reserve(ledger)
        expected = _read(ledger, period, now)
        statements = []
        ledger.db.set_trace_callback(statements.append)
        try:
            assert _read(ledger, period, now) is expected
        finally:
            ledger.db.set_trace_callback(None)
        assert statements and all(sql == 'PRAGMA data_version' for sql in statements)
        assert expected.precheck is not None


def test_reservation_rechecks_inside_transaction_and_obeys_new_caps(tmp_path, now):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        _reserve(ledger)
        c._tally(ledger)
        statements = []
        ledger.db.set_trace_callback(statements.append)
        try:
            with pytest.raises(AnthropicError, match='budget_exceeded'):
                _reserve(ledger, _cfg(daily_request_budget=1), unit='second')
        finally:
            ledger.db.set_trace_callback(None)
        assert 'BEGIN IMMEDIATE' in statements
        assert any('AND artifact_id<=' in sql for sql in statements)
        assert ledger._cloud_usage_tally.precheck is None
        assert len(ledger.artifacts(c.KINDS[0])) == 1


@pytest.mark.parametrize('period', ['month', 'window'])
@pytest.mark.parametrize('external', [False, True])
def test_same_length_mutation_rechecks_existing_fingerprint(tmp_path, now, period, external):
    path = str(tmp_path / 'synthetic.db')
    with closing(Ledger(path)) as ledger:
        _reserve(ledger)
        original = _read(ledger, period, now)
        row = ledger.db.execute('SELECT artifact_id,meta FROM artifacts WHERE kind=?', (c.KINDS[0],)).fetchone()
        changed = re.sub(r'("unit_key"\s*:\s*)"u"', r'\g<1>"v"', row['meta'])
        assert changed != row['meta'] and len(changed) == len(row['meta'])
        if external:
            with sqlite3.connect(path) as writer:
                writer.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (changed, row['artifact_id']))
        else:
            with ledger.db:
                ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (changed, row['artifact_id']))
        statements = []
        ledger.db.set_trace_callback(statements.append)
        try:
            rebuilt = _read(ledger, period, now)
            assert rebuilt is not original and rebuilt.total == original.total
        finally:
            ledger.db.set_trace_callback(None)
        assert any('AND artifact_id<=' in sql for sql in statements)


@pytest.mark.parametrize('external', [False, True])
def test_same_length_invalid_metadata_blocks_next_reservation(tmp_path, now, external):
    path = str(tmp_path / 'synthetic.db')
    with closing(Ledger(path)) as ledger:
        _reserve(ledger)
        c._tally(ledger)
        row = ledger.db.execute('SELECT artifact_id,meta FROM artifacts WHERE kind=?', (c.KINDS[0],)).fetchone()
        changed = re.sub(r'("project_id"\s*:\s*)1', r'\g<1>2', row['meta'])
        assert changed != row['meta'] and len(changed) == len(row['meta'])
        with closing(sqlite3.connect(path)) if external else nullcontext(ledger.db) as writer:
            with writer:
                writer.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (changed, row['artifact_id']))
        with pytest.raises(AnthropicError, match='pricing_unverified'):
            _reserve(ledger, unit='next')
        assert len(ledger.artifacts(c.KINDS[0])) == 1


@pytest.mark.parametrize('column', ['artifact_id', 'kind', 'project_id', 'message_id', 'model', 'meta', 'created_at'])
def test_changed_accounting_columns_rebuild_or_reject_tally(tmp_path, now, column):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        _reserve(ledger)
        original = c._tally(ledger)
        row = ledger.db.execute('SELECT * FROM artifacts WHERE kind=?', (c.KINDS[0],)).fetchone()
        changed = {'artifact_id': row['artifact_id'] + 100, 'kind': c.KINDS[1], 'project_id': 2,
                   'message_id': 2, 'model': 'invalid', 'meta': row['meta'].replace('"u"', '"v"'),
                   'created_at': now + 86400}[column]
        if column == 'message_id':
            from semantic_testkit import _message, _patient
            _patient(ledger, 1)
            ledger.save_messages([_message(2, pid=1, body='完全合成の関連付け検証。')])
        with ledger.db:
            ledger.db.execute(f'UPDATE artifacts SET {column}=? WHERE artifact_id=?', (changed, row['artifact_id']))
        if column in ('project_id', 'message_id', 'model'):
            with pytest.raises(AnthropicError, match='pricing_unverified'):
                c._tally(ledger)
        else:
            rebuilt = c._tally(ledger)
            assert rebuilt is not original
            if column == 'kind':
                assert rebuilt.invalid
            else:
                assert rebuilt.total == original.total
                if column == 'created_at':
                    assert c._periods(now)[0] not in rebuilt.days
                    assert rebuilt.days[c._periods(changed)[0]][0] == 1


@pytest.mark.parametrize('period', ['month', 'window'])
def test_unrelated_write_and_append_preserve_incremental_tally(tmp_path, monkeypatch, now, period):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        reservation = _reserve(ledger)
        original = _read(ledger, period, now)
        last_id, total = original.last_id, original.total
        decoded = []
        original_loads = c._loads

        def loads(raw):
            decoded.append(raw)
            return original_loads(raw)

        monkeypatch.setattr(c, '_loads', loads)
        ledger.artifact_add('synthetic_observation', '{}')
        assert _read(ledger, period, now) is original
        assert original.last_id == last_id and original.total == total
        assert not decoded
        ledger.artifact_add(c.KINDS[0], '{}', project_id=1, model=MODEL,
                            meta={**reservation, 'reservation_id': 'b' * 32, 'unit_key': 'other'})
        assert _read(ledger, period, now) is original
        assert original.last_id > last_id and original.total == 2 * total
        assert len(decoded) == 1


@pytest.mark.parametrize('period', ['month', 'window'])
def test_transaction_snapshot_and_rollback_never_seed_precheck(tmp_path, now, period):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        reservation = _reserve(ledger)
        original = _read(ledger, period, now).total
        ledger.db.execute('BEGIN IMMEDIATE')
        try:
            ledger.artifact_add_tx(c.KINDS[0], '{}', project_id=1, model=MODEL,
                                  meta={**reservation, 'reservation_id': 'b' * 32, 'unit_key': 'other'})
            tentative = _read(ledger, period, now)
            assert tentative.total == 2 * original and tentative.precheck is None
        finally:
            ledger.db.rollback()
        assert _read(ledger, period, now).total == original


def test_commit_during_refresh_is_not_cached_with_new_stamp(tmp_path, monkeypatch, now):
    path = str(tmp_path / 'synthetic.db')
    with closing(Ledger(path)) as ledger:
        reservation = _reserve(ledger)
        original = c._Tally.update
        committed = []

        def update(tally, owner, current):
            original(tally, owner, current)
            if not committed:
                committed.append(True)
                with sqlite3.connect(path) as writer:
                    writer.execute('INSERT INTO artifacts(kind,project_id,model,meta,content,created_at) '
                                   'VALUES(?,?,?,?,?,?)', (c.KINDS[0], 1, MODEL,
                                   json.dumps({**reservation, 'reservation_id': 'b' * 32, 'unit_key': 'other'}),
                                   '{}', now))

        monkeypatch.setattr(c._Tally, 'update', update)
        first = c._tally(ledger)
        assert first.precheck is None and ledger._cloud_usage_tally is None
        assert c._tally(ledger).total == 2 * first.total


@pytest.mark.parametrize('period', ['month', 'window'])
def test_mutation_between_fingerprint_and_update_uses_one_snapshot(tmp_path, monkeypatch, now, period):
    path = str(tmp_path / 'synthetic.db')
    with closing(Ledger(path)) as ledger:
        _reserve(ledger)
        original = _read(ledger, period, now)
        ledger.artifact_add('synthetic_observation', '{}')
        fingerprint = c._Tally.fingerprint
        committed = []

        def changed(tally, owner, current):
            result = fingerprint(tally, owner, current)
            if not committed:
                committed.append(True)
                with closing(sqlite3.connect(path)) as writer, writer:
                    writer.execute('UPDATE artifacts SET project_id=2 WHERE kind=?', (c.KINDS[0],))
            return result

        monkeypatch.setattr(c._Tally, 'fingerprint', changed)
        snapshot = _read(ledger, period, now)
        assert committed and snapshot is original and not snapshot.invalid and snapshot.total > 0
        attribute = '_cloud_usage_tally' if period == 'month' else '_cloud_window_tally'
        assert snapshot.precheck is None and getattr(ledger, attribute) is None
        with pytest.raises(AnthropicError, match='pricing_unverified'):
            _read(ledger, period, now)


@pytest.mark.parametrize('period', ['month', 'window'])
@pytest.mark.parametrize('mutation', ['binding', 'delete', 'duplicate'])
def test_prior_period_reservation_changes_invalidate_orphan_binding(tmp_path, now, period, mutation):
    path = str(tmp_path / 'synthetic.db')
    with closing(Ledger(path)) as ledger:
        _late_results(ledger, 1, c._periods(now)[1] - 1, now)
        original = _read(ledger, period, now)
        assert original.orphans and not original.invalid and original.total == 0
        row = ledger.db.execute('SELECT * FROM artifacts WHERE kind=?', (c.KINDS[0],)).fetchone()
        with closing(sqlite3.connect(path)) as writer, writer:
            if mutation == 'binding':
                changed = re.sub(r'("unit_key"\s*:\s*)"0"', r'\g<1>"1"', row['meta'])
                assert changed != row['meta'] and len(changed) == len(row['meta'])
                writer.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (changed, row['artifact_id']))
            elif mutation == 'delete':
                writer.execute('DELETE FROM artifacts WHERE artifact_id=?', (row['artifact_id'],))
            else:
                writer.execute('INSERT INTO artifacts(kind,project_id,model,meta,content,created_at) '
                               'VALUES(?,?,?,?,?,?)', (row['kind'], row['project_id'], row['model'],
                               row['meta'], '{}', row['created_at']))
        rebuilt = _read(ledger, period, now)
        assert rebuilt is not original and rebuilt.invalid and rebuilt.precheck is None


@pytest.mark.parametrize('period', ['month', 'window'])
def test_prior_reservation_retention_boundary_expires_unchanged_cache(tmp_path, monkeypatch, now, period):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        _late_results(ledger, 1, now - 62 * 86400 + 1, now)
        original = _read(ledger, period, now)
        assert not original.invalid and original.precheck is not None
        monkeypatch.setattr(c.time, 'time', lambda: now + 1)
        at_boundary = c._tally(ledger) if period == 'month' else _read(ledger, period, now)
        assert at_boundary is original
        monkeypatch.setattr(c.time, 'time', lambda: now + 2)
        expired = c._tally(ledger) if period == 'month' else _read(ledger, period, now)
        assert expired is not original and expired.invalid


@pytest.mark.parametrize('period', ['month', 'window'])
def test_clock_rollback_rechecks_previously_expired_duplicate(tmp_path, monkeypatch, now, period):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        _late_results(ledger, 1, c._periods(now)[1] - 1, now)
        with ledger.db:
            ledger.db.execute('INSERT INTO artifacts(kind,project_id,model,meta,content,created_at) '
                              'SELECT kind,project_id,model,meta,content,? FROM artifacts WHERE kind=?',
                              (now - 62 * 86400 - 1, c.KINDS[0]))
        original = _read(ledger, period, now)
        assert not original.invalid and original.precheck is not None
        monkeypatch.setattr(c.time, 'time', lambda: now - 2)
        earlier = c._tally(ledger) if period == 'month' else _read(ledger, period, now)
        assert earlier is not original and earlier.invalid


@pytest.mark.parametrize('period', ['month', 'window'])
def test_period_change_does_not_reuse_old_total(tmp_path, now, period):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        _reserve(ledger)
        assert _read(ledger, period, now).total > 0
        later = c._tally(ledger, now + 32 * 86400) if period == 'month' else c._window_tally(ledger, now + 1)
        assert later.total == 0


def test_connection_identity_is_part_of_precheck(tmp_path, now):
    with closing(Ledger(str(tmp_path / 'first.db'))) as first, closing(Ledger(str(tmp_path / 'second.db'))) as second:
        _reserve(first)
        owner = SimpleNamespace(db=first.db)
        assert c._tally(owner).total > 0
        owner.db = second.db
        assert c._tally(owner).total == 0


def test_invalid_total_is_revalidated_instead_of_cached(tmp_path, now):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        reservation = _reserve(ledger)
        ledger.artifact_add(c.KINDS[1], '{}', project_id=1, model=MODEL,
                            meta={**reservation, 'usage': {'input_tokens': -1, 'output_tokens': 2},
                                  'http_class': 'http_200', 'cost_usd': '0'})
        first = c._tally(ledger)
        second = c._tally(ledger)
        assert first.invalid and second.invalid and first is not second
        assert first.precheck is None and second.precheck is None


def test_matching_semantic_history_does_not_walk_json_twice():
    with closing(sqlite3.connect(':memory:')) as db:
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY,kind TEXT,project_id INTEGER,'
                   'message_id INTEGER,model TEXT,meta TEXT,created_at REAL)')
        db.execute('CREATE INDEX idx_artifacts_lookup ON artifacts(kind,project_id,message_id)')
        maximum = c._reservation_cost(300, 100)
        candidate = {'reservation_id': 'a' * 32, 'request_fp': 'b' * 64, 'logical_request_fp': 'c' * 64,
                     'logical_target_id': 1, 'logical_unit_fp': 'd' * 64, 'route': 'semantic_backlog',
                     'project_id': 1, 'message_id': 1, 'model': MODEL, 'price_version': c.PRICE_VERSION,
                     'unit_key': 'synthetic', 'max_cost_usd': str(maximum), 'max_tokens': 100,
                     'input_bound': 300, 'owner': {}}
        unrelated = {**candidate, 'message_id': 2, 'logical_target_id': 2}
        db.executemany('INSERT INTO artifacts(kind,project_id,message_id,model,meta,created_at) VALUES(?,?,?,?,?,?)',
                       [(c.KINDS[0], 1, 2, MODEL, json.dumps({**unrelated, 'reservation_id': f'{i:032x}'}), 0)
                        for i in range(2000)])
        steps = [0]
        db.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1) or 0, 1000)
        try:
            c._guard_repeated_request(SimpleNamespace(db=db), candidate)
        finally:
            db.set_progress_handler(None, 0)
        assert steps[0] < 300


@pytest.mark.parametrize('meta', [
    '{"route":"other","route":"semantic_backlog"}',
    '{"route":"semantic_backlog","unit_key":"other","unit_key":"synthetic"}',
    '{"route":"other","nested":{"key":1,"key":2}}',
])
def test_duplicate_keys_cannot_hide_reservation_after_predicate_reordering(tmp_path, now, meta):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        reservation = _reserve(ledger)
        with ledger.db:
            ledger.db.execute('UPDATE artifacts SET meta=? WHERE kind=?', (meta, c.KINDS[0]))
        with pytest.raises(AnthropicError, match='budget_exceeded'):
            c._guard_repeated_request(ledger, {**reservation, 'route': 'semantic_backlog'})
