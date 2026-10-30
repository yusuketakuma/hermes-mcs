"""Synthetic unchanged-read reuse never replaces transactional budget checks."""
from contextlib import closing
import json
import re
import sqlite3
from types import SimpleNamespace

import pytest

import cloud_policy as c
from anthropic_client import AnthropicError, MODEL
from ledger import Ledger
from test_cloud_policy import _cfg, _reserve, now

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
        assert any(sql.startswith('SELECT count(*), total(length(meta))') for sql in statements)
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
            assert _read(ledger, period, now) is original
        finally:
            ledger.db.set_trace_callback(None)
        assert any(sql.startswith('SELECT count(*), total(length(meta))') for sql in statements)


@pytest.mark.parametrize('period', ['month', 'window'])
def test_unrelated_write_and_append_preserve_incremental_tally(tmp_path, now, period):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        reservation = _reserve(ledger)
        original = _read(ledger, period, now)
        last_id, total = original.last_id, original.total
        ledger.artifact_add('synthetic_observation', '{}')
        assert _read(ledger, period, now) is original
        assert original.last_id == last_id and original.total == total
        ledger.artifact_add(c.KINDS[0], '{}', project_id=1, model=MODEL,
                            meta={**reservation, 'reservation_id': 'b' * 32, 'unit_key': 'other'})
        assert _read(ledger, period, now) is original
        assert original.last_id > last_id and original.total == 2 * total


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
