"""Persisted raw splits carry no provider metadata: a profile-effort change leaves
them unverifiable instead of silently rebound (fully synthetic)."""
import pytest

import cloud_inference
import extract_llm as worker
import extraction_context as frames
import openai_client
from inference_dependency_loss import source_binding
from inference_version import CURRENT_VERSION, VERSION_KEY
from test_anthropic_runtime import old, routed

__all__ = ['routed']

ATTEMPT = {'qc_src': None, 'thin_src': None, 'human_src': None, 'auto_retry': 0}


def _contract(effort):
    return cloud_inference.cloud_provider_contract(
        selection={'provider': 'openai', 'model': openai_client.MODEL, 'effort': effort})


def _frozen(ledger):
    old(ledger)
    row = dict(ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    row.update(_source_binding=source_binding(ledger.db, 1, 1),
               _target=('', openai_client.MODEL), _cloud_mode='on')
    frame = frames.prepare(ledger, row, model=openai_client.MODEL,
        contract=worker._context_contract(), attempt=ATTEMPT, enabled=False,
        base_context=None, version=worker.EXTRACT_VERSION)
    return row, frame


def _persist_split(ledger, row, frame, contract):
    # Raw split form written by the real writer: split_size but no provider_leaf.
    bound = {**row, '_provider_contract': contract, '_frame': frame}
    return ledger.artifact_add('extract_llm_chunk', '{"symptoms":[]}',
        project_id=1, message_id=1, model=openai_client.MODEL,
        meta={VERSION_KEY: CURRENT_VERSION, 'hash': row['content_hash'],
              'ver': worker.EXTRACT_VERSION, 'chunk': 0,
              'context': worker._chunk_context(bound, frame['text'], checkpoint=True),
              'chunk_size': worker._CHUNK_SIZE, 'split_size': worker._CHUNK_SIZE,
              'piece_sha256': 'a' * 64, 'actual_model': openai_client.MODEL,
              'plan_version': worker.PLAN_VERSION})


def test_same_contract_split_resumes(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist_split(ledger, row, frame, _contract('low'))
    picked = worker._pending_context_frame(
        ledger.db, {**row, '_provider_contract': _contract('low')},
        attempt=ATTEMPT, enabled=False)
    assert picked is not None and picked['artifact_id'] == frame['artifact_id']


@pytest.mark.parametrize('resume', [
    pytest.param(_contract('medium'), id='effort-changed'),
    pytest.param({'backend': 'openai', 'model': openai_client.MODEL, 'prompt': '0' * 16},
                 id='unproven-contract')])
def test_changed_contract_leaves_split_unverifiable(routed, resume):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist_split(ledger, row, frame, _contract('low'))
    with pytest.raises(ValueError, match='pending_context_unverifiable'):
        worker._pending_context_frame(
            ledger.db, {**row, '_provider_contract': resume},
            attempt=ATTEMPT, enabled=False)
