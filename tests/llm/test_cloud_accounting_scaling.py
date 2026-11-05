"""Synthetic accounting keeps full validation while avoiding repeated history scans."""
from contextlib import closing
from datetime import datetime
import json

import pytest

import cloud_usage_accounting
import cloud_policy as c
from anthropic_client import MODEL
from ledger import Ledger
from test_cloud_policy import _late_results, _reserve, now

__all__ = ['now']


@pytest.mark.parametrize('count', [40, 400, 901])
def test_late_result_tally_batches_retained_history(tmp_path, monkeypatch, now, count):
    current = datetime(2026, 11, 1, 0, 1, tzinfo=c.JST).timestamp()
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        _late_results(ledger, count, current - 120, current - 30)
        ledger.db.executemany(
            "INSERT INTO artifacts(kind,meta,created_at) VALUES('cloud_usage_reserve',?,?)",
            [(json.dumps({'reservation_id': f'{index:032x}'}), current - 86400)
             for index in range(1000)])
        ledger.db.commit()
        expected = c.usage_report(ledger, now=current)
        statements, decoded = [], []
        original = c._accounting_meta

        def decode(raw):
            decoded.append(1)
            return original(raw)

        monkeypatch.setattr(c, '_accounting_meta', decode)
        monkeypatch.setattr(cloud_usage_accounting, '_accounting_meta', decode)
        ledger.db.set_trace_callback(statements.append)
        try:
            tally = c._tally(ledger, current)
        finally:
            ledger.db.set_trace_callback(None)
        assert not tally.invalid and not expected['pricing_unverified']
        assert str(tally.total) == expected['monthly_cost_usd'] == '0'
        assert len(tally.orphans) == count
        checks = [sql for sql in statements if 'json_tree(meta)' in sql]
        lookups = [sql for sql in statements if "THEN json_extract(meta,'$.reservation_id') END IN" in sql]
        # One batch binds the results, and one independently hashes prior bindings.
        assert len(checks) == 2
        assert len(lookups) == 2 * ((count + 899) // 900)
        assert len(decoded) == 2 * count


@pytest.mark.parametrize('transaction', [False, True])
def test_unrelated_write_hashes_verified_prefix_only_once(tmp_path, monkeypatch, now, transaction):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        reservation = _reserve(ledger)
        with ledger.db:
            for index in range(1, 50):
                ledger.artifact_add_tx(c.KINDS[0], '{}', project_id=1, model=MODEL,
                    meta={**reservation, 'reservation_id': f'{index:032x}', 'unit_key': str(index)})
        original = c._tally(ledger, now)
        ledger.artifact_add('synthetic_observation', '{}')
        statements, hashed = [], []
        canonical = c.canonical

        def encode(value):
            if isinstance(value, tuple) and len(value) == 7:
                hashed.append(1)
            return canonical(value)

        monkeypatch.setattr(c, 'canonical', encode)
        ledger.db.set_trace_callback(statements.append)
        if transaction:
            ledger.db.execute('BEGIN IMMEDIATE')
        try:
            tally = c._tally(ledger, now)
        finally:
            if transaction:
                ledger.db.rollback()
            ledger.db.set_trace_callback(None)
        assert tally is original and not tally.invalid
        assert len(hashed) == 50
        assert sum('AND artifact_id<=' in sql for sql in statements) == 1
        assert sum('WHEN typeof(created_at)' in sql for sql in statements) == 1
        assert tally.precheck is None if transaction else tally.precheck is not None
