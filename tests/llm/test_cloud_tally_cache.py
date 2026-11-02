"""Synthetic unchanged-read reuse never replaces transactional budget checks."""
from contextlib import closing, nullcontext
import json
import re
import sqlite3
import time
import weakref
from types import SimpleNamespace

import pytest

import cloud_policy as c
from anthropic_client import AnthropicClient, AnthropicError, MODEL
from ledger import Ledger
from test_cloud_policy import _cfg, _late_results, _reserve, _result, now

__all__ = ['now']


def _read(ledger, period, now):
    return c._tally(ledger, now) if period == 'month' else c._window_tally(ledger, now - 10)


@pytest.mark.parametrize('completion_fails', [False, True])
def test_close_releases_accounting_caches_even_when_completed_cleanup_fails(tmp_path, now, completion_fails):
    ledger = Ledger(str(tmp_path / 'synthetic.db'))
    _reserve(ledger)
    monthly = weakref.ref(c._tally(ledger, now))
    window = weakref.ref(c._window_tally(ledger, now - 10))
    class Completed:
        def close(self):
            raise RuntimeError('synthetic_cleanup_failure')
    if completion_fails:
        ledger._patient_context_completed = Completed()
        with pytest.raises(RuntimeError, match='synthetic_cleanup_failure'):
            ledger.close()
    else:
        ledger.close()
    assert monthly() is None and window() is None
    assert '_patient_context_completed' not in ledger.__dict__
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        ledger.db.execute('SELECT 1')
    ledger.close()


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
        with pytest.raises(AnthropicError, match='budget_exceeded') as caught:
            _reserve(ledger, unit='next')
        assert caught.value.detail == 'cloud_unit_unresolved'
        assert c.usage_report(ledger)['pricing_unverified']
        assert len(ledger.artifacts(c.KINDS[0])) == 1


def _window_cfg():
    return _cfg(window_start='2026-10-26T00:00:00+09:00', window_budget_usd=20)


def _assert_unverified_without_http(ledger, cfg, *, expected_kind='pricing_unverified'):
    report = c.usage_report(ledger)
    assert report['pricing_unverified'] and report['monthly_cost_usd'] is None
    assert c.budget_block_reason(ledger, cfg) == 'pricing_unverified'
    with pytest.raises(AnthropicError, match='pricing_unverified'):
        c.window_spend(ledger, cfg)
    before = len(ledger.artifacts(c.KINDS[0]))
    calls = []
    client = AnthropicClient('synthetic', post_fn=lambda *args: calls.append(True), max_attempts=1)
    reserve = c.usage_reserver(ledger, cfg, route='reextract', project_id=1, unit_key='different')
    with pytest.raises(AnthropicError) as caught:
        client.complete('合成の入力', deadline=time.monotonic() + 30, max_tokens=100, reserve_fn=reserve)
    assert caught.value.kind == expected_kind
    if expected_kind == 'budget_exceeded':
        assert caught.value.detail == 'cloud_unit_unresolved'
    assert not calls and len(ledger.artifacts(c.KINDS[0])) == before


@pytest.mark.parametrize('kind', [c.KINDS[0], c.KINDS[1]])
@pytest.mark.parametrize('bad', [None, 'invalid-date', float('inf'), float('-inf'), 1e30, -1e30])
@pytest.mark.parametrize('external', [False, True])
def test_invalid_timestamp_in_any_accounting_row_blocks_all_budget_paths(tmp_path, now, kind, bad, external):
    path = str(tmp_path / 'synthetic.db')
    with closing(Ledger(path)) as ledger:
        reservation = _reserve(ledger)
        if kind == c.KINDS[1]:
            c.record_result(ledger, reservation, _result(reservation))
        cfg = _window_cfg()
        c._tally(ledger)
        c.window_spend(ledger, cfg)
        row = ledger.db.execute('SELECT artifact_id FROM artifacts WHERE kind=?', (kind,)).fetchone()
        with closing(sqlite3.connect(path)) if external else nullcontext(ledger.db) as writer:
            with writer:
                writer.execute('UPDATE artifacts SET created_at=? WHERE artifact_id=?', (bad, row['artifact_id']))
        _assert_unverified_without_http(ledger, cfg)
        report = c.usage_report(ledger)
        assert report['daily_requests'] is None
        saved = ledger.db.execute('SELECT created_at FROM artifacts WHERE artifact_id=?', (row['artifact_id'],)).fetchone()
        assert saved is not None and saved['created_at'] == bad


@pytest.mark.parametrize('kind', [c.KINDS[0], c.KINDS[1]])
@pytest.mark.parametrize('damage', ['duplicate', 'overflow', 'nan'])
def test_ambiguous_accounting_metadata_is_unverified(tmp_path, now, kind, damage):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        reservation = _reserve(ledger)
        if kind == c.KINDS[1]:
            c.record_result(ledger, reservation, _result(reservation))
        row = ledger.db.execute('SELECT artifact_id,meta FROM artifacts WHERE kind=?', (kind,)).fetchone()
        raw = row['meta']
        if damage == 'duplicate':
            raw = '{"project_id":2,' + raw[1:]
        else:
            raw = raw[:-1] + ',"unused":' + ('1e999' if damage == 'overflow' else 'NaN') + '}'
        with ledger.db:
            ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (raw, row['artifact_id']))
        expected = 'budget_exceeded' if kind == c.KINDS[0] else 'pricing_unverified'
        _assert_unverified_without_http(ledger, _window_cfg(), expected_kind=expected)
        assert ledger.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (row['artifact_id'],)).fetchone()[0] == raw


@pytest.mark.parametrize('damage', ['timestamp', 'duplicate', 'nonfinite'])
def test_corrupt_prior_period_orphan_reservation_is_unverified(tmp_path, now, damage):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        _late_results(ledger, 1, c._periods(now)[1] - 1, now)
        c._tally(ledger)
        row = ledger.db.execute('SELECT artifact_id,meta FROM artifacts WHERE kind=?', (c.KINDS[0],)).fetchone()
        with ledger.db:
            if damage == 'timestamp':
                ledger.db.execute('UPDATE artifacts SET created_at=NULL WHERE artifact_id=?', (row['artifact_id'],))
            else:
                raw = ('{"project_id":2,' + row['meta'][1:] if damage == 'duplicate'
                       else row['meta'][:-1] + ',"unused":1e999}')
                ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (raw, row['artifact_id']))
        expected = 'pricing_unverified' if damage == 'timestamp' else 'budget_exceeded'
        _assert_unverified_without_http(ledger, _window_cfg(), expected_kind=expected)


@pytest.mark.parametrize('bad_kind', [c.KINDS[0], c.KINDS[1]])
def test_paid_result_survives_unrelated_corrupt_timestamp_but_new_sends_stop(tmp_path, now, bad_kind):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        other = _reserve(ledger, unit='other')
        if bad_kind == c.KINDS[1]:
            c.record_result(ledger, other, _result(other))
        bad_row = ledger.db.execute('SELECT artifact_id,created_at FROM artifacts WHERE kind=?', (bad_kind,)).fetchone()
        received = []
        sent = []

        def post(body, timeout):
            sent.append(True)
            before = c.usage_report(ledger)
            assert before['unresolved_requests'] >= 1
            with ledger.db:
                ledger.db.execute('UPDATE artifacts SET created_at=NULL WHERE artifact_id=?', (bad_row['artifact_id'],))
            reply = {'model': MODEL, 'stop_reason': 'end_turn',
                     'content': [{'type': 'text', 'text': '合成の回答'}],
                     'usage': {'input_tokens': 10, 'output_tokens': 2}}
            return 200, {}, json.dumps(reply).encode()

        def save(reservation, metadata):
            received.append((reservation, metadata))
            c.record_result(ledger, reservation, metadata)

        client = AnthropicClient('synthetic', post_fn=post, max_attempts=1)
        reserve = c.usage_reserver(ledger, _cfg(), route='reextract', project_id=1, unit_key='paid')
        answer = client.complete('合成の入力', deadline=time.monotonic() + 30, max_tokens=100,
                                 reserve_fn=reserve, after_result=save)
        assert answer['text'] == '合成の回答' and sent == [True] and len(received) == 1
        paid, metadata = received[0]
        c.record_result(ledger, paid, metadata)
        rows = c._matching_bound_rows(ledger, c.KINDS[1], paid['reservation_id'])
        assert len(rows) == 1
        saved = json.loads(rows[0]['meta'])
        cost, settled = c._settled(paid, c._decimal(paid['max_cost_usd']), [(saved, rows[0])])
        assert settled and cost == c.usage_cost(metadata['usage'], model=MODEL)
        assert c.usage_report(ledger)['pricing_unverified']
        assert c.budget_block_reason(ledger, _cfg()) == 'pricing_unverified'
        with pytest.raises(AnthropicError, match='pricing_unverified'):
            client.complete('新しい合成入力', deadline=time.monotonic() + 30, max_tokens=100,
                            reserve_fn=c.usage_reserver(ledger, _cfg(), route='reextract',
                                                        project_id=1, unit_key='next'))
        assert sent == [True] and len(ledger.artifacts(c.KINDS[0])) == 2
        assert ledger.db.execute('SELECT created_at FROM artifacts WHERE artifact_id=?',
                                 (bad_row['artifact_id'],)).fetchone()[0] is None
        with ledger.db:
            ledger.db.execute('UPDATE artifacts SET created_at=? WHERE artifact_id=?',
                              (bad_row['created_at'], bad_row['artifact_id']))
        report = c.usage_report(ledger)
        other_cost = (c._decimal(other['max_cost_usd']) if bad_kind == c.KINDS[0]
                      else c.usage_cost(_result(other)['usage'], model=MODEL))
        assert not report['pricing_unverified']
        assert c._decimal(report['monthly_cost_usd']) == cost + other_cost
        assert report['unresolved_requests'] == (1 if bad_kind == c.KINDS[0] else 0)


@pytest.mark.parametrize('kind', [c.KINDS[0], c.KINDS[1]])
@pytest.mark.parametrize('damage', ['timestamp', 'duplicate', 'duplicate_key', 'blob', 'oversized'])
def test_result_rejects_damage_to_its_own_reservation_or_result(tmp_path, now, kind, damage):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        reservation = _reserve(ledger)
        metadata = _result(reservation)
        if kind == c.KINDS[1]:
            c.record_result(ledger, reservation, metadata)
        row = ledger.db.execute('SELECT artifact_id,meta FROM artifacts WHERE kind=?', (kind,)).fetchone()
        with ledger.db:
            if damage == 'timestamp':
                ledger.db.execute('UPDATE artifacts SET created_at=NULL WHERE artifact_id=?', (row['artifact_id'],))
            elif damage == 'duplicate':
                ledger.db.execute('INSERT INTO artifacts(kind,project_id,message_id,model,content,meta,created_at) '
                                  'SELECT kind,project_id,message_id,model,content,meta,created_at '
                                  'FROM artifacts WHERE artifact_id=?', (row['artifact_id'],))
            elif damage == 'duplicate_key':
                raw = '{"reservation_id":"' + 'f' * 32 + '",' + row['meta'][1:]
                ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (raw, row['artifact_id']))
            elif damage == 'blob':
                raw = b'{"reservation_id":"' + reservation['reservation_id'].encode() + b'"\xff'
                ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (raw, row['artifact_id']))
            else:
                raw = row['meta'][:-1] + ',"padding":"' + 'x' * 32768 + '"}'
                ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (raw, row['artifact_id']))
        before = [tuple(r) for r in ledger.db.execute('SELECT * FROM artifacts WHERE kind IN (?,?) ORDER BY artifact_id',
                                                       c.KINDS[:2])]
        with pytest.raises(AnthropicError, match='pricing_unverified'):
            c.record_result(ledger, reservation, metadata)
        after = [tuple(r) for r in ledger.db.execute('SELECT * FROM artifacts WHERE kind IN (?,?) ORDER BY artifact_id',
                                                      c.KINDS[:2])]
        assert after == before


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
        original_loads = c._accounting_meta

        def loads(raw):
            decoded.append(raw)
            return original_loads(raw)

        monkeypatch.setattr(c, '_accounting_meta', loads)
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


def test_concurrent_precheck_commit_cannot_bypass_transactional_cap_or_open_circuit(tmp_path, monkeypatch, now):
    path = str(tmp_path / 'synthetic.db')
    with closing(Ledger(path)) as ledger:
        reservation = _reserve(ledger)
        c._tally(ledger)
        ledger.artifact_add('synthetic_observation', '{}')
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
        cfg = _cfg(daily_request_budget=2)
        assert c.budget_block_reason(ledger, cfg) is None
        assert committed and ledger._cloud_usage_tally is None
        assert c.global_block_reason(ledger, model=MODEL) is None
        calls = []
        client = AnthropicClient('synthetic', post_fn=lambda *args: calls.append(True), max_attempts=1)
        reserve = c.usage_reserver(ledger, cfg, route='reextract', project_id=1, unit_key='third')
        with pytest.raises(AnthropicError, match='budget_exceeded'):
            client.complete('合成の入力', deadline=time.monotonic() + 30, max_tokens=100, reserve_fn=reserve)
        assert not calls and len(ledger.artifacts(c.KINDS[0])) == 2
        assert c.global_block_reason(ledger, model=MODEL) is None


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


@pytest.mark.parametrize('identified', [True, False])
def test_matching_semantic_history_does_not_walk_json_twice(identified):
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
                     'input_bound': 300, 'owner': {'semantic_target_id': 1} if identified else {}}
        unrelated = {**candidate, 'message_id': 2, 'logical_target_id': 2,
                     'owner': {'semantic_target_id': 2} if identified else {}}
        db.executemany('INSERT INTO artifacts(kind,project_id,message_id,model,meta,created_at) VALUES(?,?,?,?,?,?)',
                       [(c.KINDS[0], 1, 2, MODEL, json.dumps({**unrelated, 'reservation_id': f'{i:032x}'}), 0)
                        for i in range(2000)])
        steps = [0]
        db.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1) or 0, 1000)
        try:
            if identified:
                c._guard_repeated_request(SimpleNamespace(db=db), candidate)
            else:
                with pytest.raises(AnthropicError, match='budget_exceeded') as caught:
                    c._guard_repeated_request(SimpleNamespace(db=db), candidate)
                assert caught.value.detail == 'cloud_unit_unresolved'
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


@pytest.mark.parametrize('raw', ['{}', b'{"amount":1.25}', '{"nested":{"value":2}}'])
def test_accounting_metadata_decodes_once(monkeypatch, raw):
    loads = json.loads
    calls = []

    def counted(*args, **kwargs):
        calls.append(True)
        return loads(*args, **kwargs)

    monkeypatch.setattr(json, 'loads', counted)
    assert c._accounting_meta(raw) == loads(raw)
    assert calls == [True]


@pytest.mark.parametrize('raw', [None, '[]', 'null', '1', '"text"', '{',
                                      '{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}', b'\xff'])
def test_accounting_metadata_preserves_strict_object_boundary(raw):
    with pytest.raises(AnthropicError) as caught:
        c._accounting_meta(raw)
    assert (caught.value.kind, caught.value.detail) == ('pricing_unverified', 'usage_invalid')


@pytest.mark.parametrize('interrupted', [False, True])
def test_incremental_tally_streams_and_closes_rows(tmp_path, now, interrupted):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        _reserve(ledger, unit='first')
        _reserve(ledger, unit='second')
        expected = c.usage_report(ledger, now=now)
        cursors = []

        class Rows:
            def __init__(self, cursor):
                self.cursor, self.read, self.closed = cursor, 0, False

            def __iter__(self):
                return self

            def __next__(self):
                if interrupted and self.read == 1:
                    raise sqlite3.OperationalError('interrupted')
                self.read += 1
                return next(self.cursor)

            def fetchall(self):
                pytest.fail('accounting rows must not be materialized')

            def close(self):
                self.closed = True
                self.cursor.close()

        class Connection:
            def __getattr__(self, name):
                return getattr(ledger.db, name)

            def execute(self, sql, *args):
                cursor = ledger.db.execute(sql, *args)
                if 'AND artifact_id>? ORDER BY artifact_id' in sql:
                    cursor = Rows(cursor)
                    cursors.append(cursor)
                return cursor

        reader = SimpleNamespace(db=Connection())
        if interrupted:
            with pytest.raises(sqlite3.OperationalError, match='interrupted'):
                c._tally(reader, now)
            assert reader._cloud_usage_tally is None
        else:
            tally = c._tally(reader, now)
            assert str(tally.total) == expected['monthly_cost_usd']
            assert len(tally.open) == expected['unresolved_requests'] == 2
        assert len(cursors) == 1 and cursors[0].closed
        assert not ledger.db.in_transaction
