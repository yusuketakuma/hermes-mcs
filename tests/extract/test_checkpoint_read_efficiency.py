"""Synthetic resume work preserves exact pieces and the post-claim read boundary."""
import extract_llm_context
import extract_llm_engine
import extract_llm_prepare
import extract_llm_progress
import extract_llm_proofs
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_store
import pytest

import extract_llm as llm
from test_chunk_dedup_growth import source as source, _completed


def test_saved_reader_plans_roots_once_and_preserves_all_leaves(source, monkeypatch):
    ledger, row = source
    size = 50
    roots = llm.plan_chunks(row['body_text'], size)
    assert len(roots) > 2
    for key in range(len(roots)):
        assert llm._persist_chunks(ledger, row, {key: _completed()}, chunk_size=size)
    expected = llm._saved_chunk_state(ledger.db, row, chunk_size=size)
    plan, calls = llm.plan_chunks, []
    def counted(body, limit):
        calls.append((body, limit))
        return plan(body, limit)
    monkeypatch.setattr(llm, 'plan_chunks', counted)
    monkeypatch.setattr(extract_llm_engine, 'plan_chunks', llm.plan_chunks)
    monkeypatch.setattr(extract_llm_prepare, 'plan_chunks', llm.plan_chunks)
    monkeypatch.setattr(extract_llm_progress, 'plan_chunks', llm.plan_chunks)
    monkeypatch.setattr(extract_llm_proofs, 'plan_chunks', llm.plan_chunks)
    assert llm._saved_chunk_state(ledger.db, row, chunk_size=size) == expected
    assert calls.count((row['body_text'], size)) == 1


@pytest.mark.parametrize('key', [0, 1, '1:0', '1:1', '1:1:0', '1:1:1', -1, '01:0', '8:0'])
def test_prepared_roots_keep_unicode_offsets_and_invalid_parent_rules(key):
    body = '合成あいうえお\n' * 12
    size = 40
    roots = llm.plan_chunks(body, size)
    offsets, end = [], 0
    for part in roots:
        offsets.append(end)
        end += len(part)
    for splits in ({}, {1: 15}, {1: 15, '1:1': 5}, {1: 0}):
        expected = llm._chunk_span(body, size, key, splits)
        actual = llm._chunk_span(body, size, key, splits, root_plan=(roots, offsets))
        assert actual == expected
        if actual is not None:
            piece, start = actual
            assert body[start:start + len(piece)] == piece


@pytest.mark.parametrize('claimed', [False, True])
def test_resident_loads_checkpoints_only_after_a_successful_claim(source, monkeypatch, claimed):
    ledger, _ = source
    monkeypatch.setattr(llm, 'load_config', lambda: {
        'extract': {'patient_state_context': False, 'prefilter': False},
        'local_llm': {'model': 'synthetic-model'},
    })
    monkeypatch.setattr(extract_llm_context, 'load_config', llm.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', llm.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', llm.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', llm.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', llm.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', llm.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', llm.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', llm.load_config)
    reads, original = [], llm._saved_chunk_state
    def read(db, row, *args, **kwargs):
        assert row.get('_claim_token')
        reads.append(row['message_id'])
        return original(db, row, *args, **kwargs)
    monkeypatch.setattr(llm, '_saved_chunk_state', read)
    monkeypatch.setattr(extract_llm_prepare, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_progress, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_proofs, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_store, '_saved_chunk_state', llm._saved_chunk_state)
    if not claimed:
        monkeypatch.setattr(llm, '_claim', lambda *args: None)
        monkeypatch.setattr(extract_llm_runner, '_claim', llm._claim)
    monkeypatch.setattr(llm, '_llm_call', lambda *args, **kwargs: llm._DEFERRED)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', llm._llm_call)
    result = llm.run_pending(ledger, limit=1, budget_s=200, workers=1, allow_cloud=False)
    assert result['done'] == 0
    assert bool(reads) is claimed
