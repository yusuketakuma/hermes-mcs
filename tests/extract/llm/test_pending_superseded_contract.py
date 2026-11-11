"""Chunk records written under a retired contract generation classify as
superseded — never resumed, never silently unverifiable (fully synthetic)."""
import pytest

import extract_llm as worker
import extract_llm_historical
import extraction_context as frames
from inference_dependency_loss import source_binding
from inference_version import CURRENT_VERSION, VERSION_KEY
from test_anthropic_runtime import old, routed

__all__ = ['routed']

ATTEMPT = {'qc_src': None, 'thin_src': None, 'human_src': None, 'auto_retry': 0}


def _frozen(ledger):
    old(ledger)
    row = dict(ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    row.update(_source_binding=source_binding(ledger.db, 1, 1),
               _target=('', 'synthetic-model'), _cloud_mode='off')
    frame = frames.prepare(ledger, row, model='synthetic-model',
        contract=worker._context_contract(), attempt=ATTEMPT, enabled=False,
        base_context=None, version=worker.EXTRACT_VERSION)
    return row, frame


def _persist(ledger, row, frame, context):
    return ledger.artifact_add('extract_llm_chunk', '{}',
        project_id=1, message_id=1, model='synthetic-model',
        meta={VERSION_KEY: CURRENT_VERSION, 'hash': row['content_hash'],
              'ver': worker.EXTRACT_VERSION, 'chunk': 0, 'context': context,
              'chunk_size': worker._CHUNK_SIZE, 'split_size': worker._CHUNK_SIZE // 3,
              'piece_sha256': 'a' * 64, 'actual_model': 'synthetic-model',
              'plan_version': worker.PLAN_VERSION})


def _current_context(row, frame):
    return worker._chunk_context({**row, '_frame': frame}, frame['text'], checkpoint=True)


def _old_context(row, frame, index=0):
    _gen_id, prompt_head, schema = extract_llm_historical.known_old_contracts()[index]
    return worker._chunk_context({**row, '_frame': frame}, frame['text'], checkpoint=True,
                                 prompt_head=prompt_head, schema=schema)


def test_retired_generation_checkpoint_is_superseded(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame))
    with pytest.raises(ValueError, match='pending_context_superseded'):
        worker._pending_context_frame(ledger.db, row, attempt=ATTEMPT, enabled=False)


def test_second_retired_generation_checkpoint_is_superseded(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame, index=1))
    with pytest.raises(ValueError, match='pending_context_superseded'):
        worker._pending_context_frame(ledger.db, row, attempt=ATTEMPT, enabled=False)


def test_unknown_checkpoint_stays_unverifiable(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, 'f' * 64)
    with pytest.raises(ValueError, match='pending_context_unverifiable'):
        worker._pending_context_frame(ledger.db, row, attempt=ATTEMPT, enabled=False)


def test_partially_recognized_records_stay_unverifiable(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame))
    _persist(ledger, row, frame, 'f' * 64)
    with pytest.raises(ValueError, match='pending_context_unverifiable'):
        worker._pending_context_frame(ledger.db, row, attempt=ATTEMPT, enabled=False)


def test_current_checkpoint_still_resumes(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _current_context(row, frame))
    picked = worker._pending_context_frame(ledger.db, row, attempt=ATTEMPT, enabled=False)
    assert picked is not None and picked['artifact_id'] == frame['artifact_id']


def test_mixed_current_and_retired_records_stay_unverifiable(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _current_context(row, frame))
    _persist(ledger, row, frame, _old_context(row, frame))
    with pytest.raises(ValueError, match='pending_context_unverifiable'):
        worker._pending_context_frame(ledger.db, row, attempt=ATTEMPT, enabled=False)
