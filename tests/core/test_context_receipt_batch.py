"""Synthetic staged repair units use one financial scan per snapshot."""
import json

import pytest

import ledger_schema
import patient_context_artifacts as artifacts
from test_inference_dependency_loss import (
    _context_receipt, _reseal_context, _sealed_context, row, save, store,
)

__all__ = ['store']


def test_candidate_checks_multiple_units_with_one_global_receipt_scan(store):
    save(store, 1, '本人は合成の生活環境を確認しました。')
    source = row(store, 1)
    fixture = _sealed_context(store, source, [])
    original = store.db.execute('SELECT content,meta FROM artifacts WHERE artifact_id=?',
                                (fixture['candidate'],)).fetchone()
    content, meta = json.loads(original['content']), json.loads(original['meta'])
    boundary = len(source['body_text']) // 2
    content['chunk_span'] = [0, boundary]
    with store.db:
        store.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?',
                         (json.dumps(content), fixture['candidate']))
    unit_key = 'c' * 64
    aid, reservation, witness = _context_receipt(store, source, fixture['ref']['repair_input_key'], unit_key)
    other_meta = {**meta, 'unit_key': unit_key, 'reservation_artifact_id': aid,
                  'reservation_id': reservation['reservation_id'], 'request_fp': reservation['request_fp']}
    other_content = {**content, 'chunk_span': [boundary, len(source['body_text'])], 'receipt_sig': witness}
    store.artifact_add(artifacts.CANDIDATE_KIND, json.dumps(other_content), project_id=1, message_id=1,
                       model=other_meta['model'], meta=other_meta)
    _reseal_context(store, fixture)
    statements = []
    before = store.db.total_changes
    store.db.set_trace_callback(statements.append)
    try:
        selected = artifacts.candidate_rows(store.db, 1, 1, fixture['ref'],
                                             source_binding=fixture['origin']['source_binding'])
    finally:
        store.db.set_trace_callback(None)
    assert len(selected) == 2 and store.db.total_changes == before
    # Vouched receipts are read through the reservation index: one lookup per receipt kind, not per unit.
    assert _indexed_lookups(statements) == len(ledger_schema.CLOUD_USAGE_KINDS)
    assert _global_receipt_scans(statements) == 0
    foreign = store.artifact_add('cloud_usage_disposition', '{}', project_id=2)
    with store.db:
        store.db.execute("UPDATE artifacts SET meta='{broken' WHERE artifact_id=?", (foreign,))
    statements.clear()
    store.db.set_trace_callback(statements.append)
    try:
        with pytest.raises(ValueError):
            artifacts.candidate_rows(store.db, 1, 1, fixture['ref'],
                                     source_binding=fixture['origin']['source_binding'])
    finally:
        store.db.set_trace_callback(None)
    # A row the guard no longer vouches for falls back to one global scan for both units.
    assert _indexed_lookups(statements) == 0 and _global_receipt_scans(statements) == 1


def _indexed_lookups(statements):
    return sum(f'INDEXED BY {ledger_schema.CLOUD_USAGE_INDEX}' in sql for sql in statements)


def _global_receipt_scans(statements):
    return sum('FROM artifacts WHERE kind IN' in sql and 'json_tree(meta)' in sql for sql in statements)
